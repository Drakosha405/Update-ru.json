from __future__ import annotations
from pathlib import Path
from enum import Enum
from typing import Any, NamedTuple
from PyQt5.QtCore import QObject, QUuid, pyqtSignal
from PyQt5.QtGui import QImage
import uuid

from . import eventloop, workflow, util
from .api import TextInput, WorkflowKind, WorkflowInput, InpaintMode, InpaintParams, FillMode
from .util import ensure, client_logger as log
from .settings import settings
from .network import NetworkError
from .image import Extent, Image, Mask, Bounds
from .client import ClientMessage, ClientEvent, filter_supported_styles, resolve_sd_version
from .document import Document, LayerObserver
from .pose import Pose
from .style import Style, Styles, SDVersion
from .connection import Connection
from .properties import Property, ObservableProperties
from .jobs import Job, JobKind, JobQueue, JobState
from .control import ControlLayer, ControlLayerList
from .resources import ControlMode
from .resolution import compute_bounds
import krita


class Workspace(Enum):
    generation = 0
    upscaling = 1
    live = 2
    animation = 3


class Model(QObject, ObservableProperties):
    """Represents diffusion workflows for a specific Krita document. Stores all inputs related to
    image generation. Launches generation jobs. Listens to server messages and keeps a
    list of finished, currently running and enqueued jobs.
    """

    _doc: Document
    _connection: Connection
    _layer: krita.Node | None = None
    _layers: LayerObserver

    workspace = Property(Workspace.generation, setter="set_workspace", persist=True)
    style = Property(Styles.list().default, persist=True)
    prompt = Property("", persist=True)
    negative_prompt = Property("", persist=True)
    control: ControlLayerList
    strength = Property(1.0, persist=True)
    batch_count = Property(1, persist=True)
    seed = Property(0, persist=True)
    fixed_seed = Property(False, persist=True)
    queue_front = Property(False, persist=True)
    inpaint: CustomInpaint
    upscale: "UpscaleWorkspace"
    live: "LiveWorkspace"
    animation: "AnimationWorkspace"
    progress = Property(0.0)
    jobs: JobQueue
    error = Property("")

    workspace_changed = pyqtSignal(Workspace)
    style_changed = pyqtSignal(Style)
    prompt_changed = pyqtSignal(str)
    negative_prompt_changed = pyqtSignal(str)
    strength_changed = pyqtSignal(float)
    batch_count_changed = pyqtSignal(int)
    seed_changed = pyqtSignal(int)
    fixed_seed_changed = pyqtSignal(bool)
    queue_front_changed = pyqtSignal(bool)
    progress_changed = pyqtSignal(float)
    error_changed = pyqtSignal(str)
    has_error_changed = pyqtSignal(bool)
    modified = pyqtSignal(QObject, str)

    def __init__(self, document: Document, connection: Connection):
        super().__init__()
        self._doc = document
        self._layers = document.create_layer_observer()
        self._connection = connection
        self.generate_seed()
        self.jobs = JobQueue()
        self.inpaint = CustomInpaint()
        self.control = ControlLayerList(self)
        self.upscale = UpscaleWorkspace(self)
        self.live = LiveWorkspace(self)
        self.animation = AnimationWorkspace(self)

        self.jobs.selection_changed.connect(self.update_preview)
        self.error_changed.connect(lambda: self.has_error_changed.emit(self.has_error))

        if client := connection.client_if_connected:
            self.style = next(iter(filter_supported_styles(Styles.list(), client)), self.style)
            self.upscale.upscaler = client.default_upscaler

    def generate(self):
        """Enqueue image generation for the current setup."""
        ok, msg = self._doc.check_color_mode()
        if not ok and msg:
            self.report_error(msg)
            return

        workflow_kind = WorkflowKind.generate if self.strength == 1.0 else WorkflowKind.refine
        client = self._connection.client
        image = None
        inpaint = None
        extent = self._doc.extent
        mask = self._doc.create_mask_from_selection(
            **get_selection_modifiers(self.inpaint.mode), min_size=64
        )
        image_bounds = compute_bounds(extent, mask.bounds if mask else None, self.strength)
        image_bounds = self.inpaint.get_context(self, mask) or image_bounds

        control = [c.get_image(image_bounds) for c in self.control]

        if mask is not None or self.strength < 1.0:
            image = self._get_current_image(image_bounds)

        if mask is not None:
            if workflow_kind is WorkflowKind.generate:
                workflow_kind = WorkflowKind.inpaint
            elif workflow_kind is WorkflowKind.refine:
                workflow_kind = WorkflowKind.refine_region

            sd_version = resolve_sd_version(self.style, client)
            inpaint_mode = self.resolve_inpaint_mode()
            if inpaint_mode is InpaintMode.custom:
                inpaint = self.inpaint.get_params(mask)
            else:
                inpaint = workflow.detect_inpaint(
                    inpaint_mode, mask.bounds, sd_version, self.prompt, control, self.strength
                )

        input = workflow.prepare(
            workflow_kind,
            image or extent,
            TextInput(self.prompt, self.negative_prompt, self.style.style_prompt),
            self.style,
            self.seed if self.fixed_seed else workflow.generate_seed(),
            client,
            mask=mask,
            strength=self.strength,
            control=control,
            inpaint=inpaint,
        )
        self.clear_error()
        eventloop.run(_report_errors(self, self._generate(input, image_bounds, self.batch_count)))

    async def _generate(self, input: WorkflowInput, bounds: Bounds, count: int = 1, is_live=False):
        if input.inpaint is not None:
            b = input.inpaint.target_bounds
            # Compute mask bounds relative to cropped image, passed to workflow
            input.inpaint.target_bounds = Bounds(b.x - bounds.x, b.y - bounds.y, *b.extent)
            bounds = b  # Also keep absolute mask bounds, to insert result image into canvas

        job_kind = JobKind.live_preview if is_live else JobKind.diffusion
        pos, neg = ensure(input.text).positive, ensure(input.text).negative
        sampling = ensure(input.sampling)
        result: list[Job] = []
        for i in range(count):
            job = self.jobs.add(job_kind, pos, neg, bounds, sampling.strength, sampling.seed)
            await self._enqueue_job(job, input)
            sampling.seed = sampling.seed + (i + 1) * settings.batch_size
            result.append(job)
        return result

    async def _enqueue_job(self, job: Job, inputs: WorkflowInput):
        if not self.jobs.any_executing():
            self.progress = 0.0

        client = self._connection.client
        work = workflow.create(inputs, client)
        job.id = await client.enqueue(work, self.queue_front)

    def upscale_image(self):
        params = self.upscale.params
        image = self._doc.get_image(Bounds(0, 0, *self._doc.extent))
        client = self._connection.client
        upscaler = params.upscaler or client.default_upscaler
        if params.use_diffusion:
            inputs = workflow.prepare(
                WorkflowKind.upscale_tiled,
                image,
                TextInput("4k uhd"),
                self.style,
                params.seed,
                client,
                strength=params.strength,
                upscale_factor=params.factor,
                upscale_model=upscaler,
            )
        else:
            inputs = workflow.prepare_upscale_simple(image, upscaler, params.factor)
        job = self.jobs.add_upscale(Bounds(0, 0, *self.upscale.target_extent), params.seed)
        self.clear_error()
        eventloop.run(_report_errors(self, self._enqueue_job(job, inputs)))

    def generate_live(self):
        strength = self.live.strength
        workflow_kind = WorkflowKind.generate if strength == 1.0 else WorkflowKind.refine
        client = self._connection.client
        ver = resolve_sd_version(self.style, client)

        image = None
        mask = self._doc.create_mask_from_selection(
            grow=settings.selection_feather / 200,  # don't apply grow for live mode
            feather=settings.selection_feather / 100,
            padding=settings.selection_padding / 100,
            min_size=512 if ver is SDVersion.sd15 else 1024,
            square=True,
        )
        bounds = Bounds(0, 0, *self._doc.extent)
        if mask is not None:
            workflow_kind = WorkflowKind.refine_region
            bounds = mask.bounds
        if mask is not None or self.live.strength < 1.0:
            image = self._get_current_image(bounds)

        input = workflow.prepare(
            workflow_kind,
            image or bounds.extent,
            TextInput(self.prompt, self.negative_prompt, self.style.style_prompt),
            self.style,
            self.seed,
            client,
            mask=mask,
            control=[c.get_image(bounds) for c in self.control],
            strength=self.live.strength,
            inpaint=InpaintParams(InpaintMode.fill, mask.bounds) if mask else None,
            is_live=True,
        )
        self.clear_error()
        eventloop.run(_report_errors(self, self._generate(input, bounds, 1, is_live=True)))

    def _get_current_image(self, bounds: Bounds):
        exclude = [  # exclude control layers from projection
            c.layer for c in self.control if not c.mode.is_part_of_image
        ]
        if self._layer:  # exclude preview layer
            exclude.append(self._layer)
        return self._doc.get_image(bounds, exclude_layers=exclude)

    def generate_control_layer(self, control: ControlLayer):
        ok, msg = self._doc.check_color_mode()
        if not ok and msg:
            self.report_error(msg)
            return

        image = self._doc.get_image(Bounds(0, 0, *self._doc.extent))
        mask = self.document.create_mask_from_selection(0, 0, padding=0.25, multiple=64)
        bounds = mask.bounds if mask else None
        input = workflow.prepare_create_control_image(image, control.mode, bounds)

        job = self.jobs.add_control(control, Bounds(0, 0, *image.extent))
        self.clear_error()
        eventloop.run(_report_errors(self, self._enqueue_job(job, input)))
        return job

    def cancel(self, active=False, queued=False):
        if queued:
            to_remove = [job for job in self.jobs if job.state is JobState.queued]
            if len(to_remove) > 0:
                self._connection.clear_queue()
                for job in to_remove:
                    self.jobs.remove(job)
        if active and self.jobs.any_executing():
            self._connection.interrupt()

    def report_progress(self, value):
        self.progress = value

    def report_error(self, message: str):
        self.error = message
        self.live.is_active = False

    def clear_error(self):
        if self.error != "":
            self.error = ""

    def handle_message(self, message: ClientMessage):
        job = self.jobs.find(message.job_id)
        if job is None:
            util.client_logger.error(f"Received message {message} for unknown job.")
            return

        if message.event is ClientEvent.progress:
            self.jobs.notify_started(job)
            self.report_progress(message.progress)
        elif message.event is ClientEvent.finished:
            if message.images:
                self.jobs.set_results(job, message.images)
            if job.kind is JobKind.control_layer:
                assert job.control is not None
                job.control.layer_id = self.add_control_layer(job, message.result).uniqueId()
            elif job.kind is JobKind.upscaling:
                self.add_upscale_layer(job)
            self.progress = 1
            self.jobs.notify_finished(job)
            if job.kind is not JobKind.diffusion:
                self.jobs.remove(job)
            elif settings.auto_preview and self._layer is None and job.id:
                self.jobs.select(job.id, 0)
        elif message.event is ClientEvent.interrupted:
            self.jobs.notify_cancelled(job)
            self.report_progress(0)
        elif message.event is ClientEvent.error:
            self.jobs.notify_cancelled(job)
            self.report_error(f"Server execution error: {message.error}")

    def update_preview(self):
        if selection := self.jobs.selection:
            self.show_preview(selection.job, selection.image)
        else:
            self.hide_preview()

    def show_preview(self, job_id: str, index: int, name_prefix="Preview"):
        job = self.jobs.find(job_id)
        assert job is not None, "Cannot show preview, invalid job id"
        name = f"[{name_prefix}] {job.params.prompt}"
        if self._layer and self._layer not in self.layers:
            self._layer = None  # layer was removed by user
        if self._layer is not None:
            self._layer.setName(name)
            self._doc.set_layer_content(self._layer, job.results[index], job.params.bounds)
            self._doc.move_to_top(self._layer)
        else:
            self._layer = self._doc.insert_layer(
                name, job.results[index], job.params.bounds, make_active=False
            )
            self._layer.setLocked(True)

    def hide_preview(self):
        if self._layer is not None:
            self._doc.hide_layer(self._layer)

    def apply_result(self, job_id: str, index: int):
        self.jobs.select(job_id, index)
        assert self._layer is not None
        self._layer.setLocked(False)
        self._layer.setName(self._layer.name().replace("[Preview]", "[Generated]"))
        self._doc.active_layer = self._layer
        self._layer = None
        self.jobs.selection = None
        self.jobs.notify_used(job_id, index)

    def add_control_layer(self, job: Job, result: dict | None):
        assert job.kind is JobKind.control_layer and job.control
        if job.control.mode is ControlMode.pose and result is not None:
            pose = Pose.from_open_pose_json(result)
            pose.scale(job.params.bounds.extent)
            return self._doc.insert_vector_layer(job.params.prompt, pose.to_svg())
        elif len(job.results) > 0:
            return self._doc.insert_layer(job.params.prompt, job.results[0], job.params.bounds)
        return self.document.active_layer  # Execution was cached and no image was produced

    def add_upscale_layer(self, job: Job):
        assert job.kind is JobKind.upscaling
        assert len(job.results) > 0, "Upscaling job did not produce an image"
        if self._layer:
            self._layer.remove()
            self._layer = None
        self._doc.resize(job.params.bounds.extent)
        self.upscale.target_extent_changed.emit(self.upscale.target_extent)
        self._doc.insert_layer(job.params.prompt, job.results[0], job.params.bounds)

    def set_workspace(self, workspace: Workspace):
        if self.workspace is Workspace.live:
            self.live.is_active = False
        self._workspace = workspace
        self.workspace_changed.emit(workspace)
        self.modified.emit(self, "workspace")

    def generate_seed(self):
        self.seed = workflow.generate_seed()

    def save_result(self, job_id: str, index: int):
        _save_job_result(self, self.jobs.find(job_id), index)

    def resolve_inpaint_mode(self):
        if self.inpaint.mode is InpaintMode.automatic:
            if bounds := self.document.selection_bounds:
                return workflow.detect_inpaint_mode(self.document.extent, bounds)
            return InpaintMode.fill
        return self.inpaint.mode

    @property
    def history(self):
        return (job for job in self.jobs if job.state is JobState.finished)

    @property
    def has_error(self):
        return self.error != ""

    @property
    def document(self):
        return self._doc

    @document.setter
    def document(self, doc):
        # Note: for some reason Krita sometimes creates a new object for an existing document.
        # The old object is deleted and unusable. This method is used to update the object,
        # but doesn't actually change the document identity.
        # TODO: 04/02/2024 is this still necessary? check log.
        assert doc == self._doc, "Cannot change document of model"
        if self._doc is not doc:
            log.warning(f"Document instance changed {self._doc} -> {doc}")
            self._doc = doc

    @property
    def layers(self):
        return self._layers


class InpaintContext(Enum):
    automatic = 0
    mask_bounds = 1
    entire_image = 2
    layer_bounds = 3


class CustomInpaint(QObject, ObservableProperties):
    mode = Property(InpaintMode.automatic, persist=True)
    fill = Property(FillMode.neutral, persist=True)
    use_inpaint = Property(True, persist=True)
    use_prompt_focus = Property(False, persist=True)
    context = Property(InpaintContext.automatic, persist=True)
    context_layer_id = Property(QUuid(), persist=True)

    mode_changed = pyqtSignal(InpaintMode)
    fill_changed = pyqtSignal(FillMode)
    use_inpaint_changed = pyqtSignal(bool)
    use_prompt_focus_changed = pyqtSignal(bool)
    context_changed = pyqtSignal(InpaintContext)
    context_layer_id_changed = pyqtSignal(QUuid)
    modified = pyqtSignal(QObject, str)

    def get_params(self, mask: Mask):
        params = InpaintParams(self.mode, mask.bounds, self.fill)
        params.use_inpaint_model = self.use_inpaint
        params.use_condition_mask = self.use_prompt_focus
        return params

    def get_context(self, model: Model, mask: Mask | None):
        if mask is None or self.mode is not InpaintMode.custom:
            return None
        if self.context is InpaintContext.mask_bounds:
            return mask.bounds
        if self.context is InpaintContext.entire_image:
            return Bounds(0, 0, *model.document.extent)
        if self.context is InpaintContext.layer_bounds:
            if layer := model.layers.find(self.context_layer_id):
                layer_bounds = Bounds.from_qrect(layer.bounds())
                return Bounds.expand(layer_bounds, include=mask.bounds)
        return None


class UpscaleParams(NamedTuple):
    upscaler: str
    factor: float
    use_diffusion: bool
    strength: float
    target_extent: Extent
    seed: int


class UpscaleWorkspace(QObject, ObservableProperties):
    upscaler = Property("", persist=True)
    factor = Property(2.0, persist=True)
    use_diffusion = Property(True, persist=True)
    strength = Property(0.3, persist=True)

    upscaler_changed = pyqtSignal(str)
    factor_changed = pyqtSignal(float)
    use_diffusion_changed = pyqtSignal(bool)
    strength_changed = pyqtSignal(float)
    target_extent_changed = pyqtSignal(Extent)
    modified = pyqtSignal(QObject, str)

    _model: Model

    def __init__(self, model: Model):
        super().__init__()
        self._model = model
        self.factor_changed.connect(lambda _: self.target_extent_changed.emit(self.target_extent))
        self._init_model()
        model._connection.models_changed.connect(self._init_model)

    def _init_model(self):
        if self.upscaler == "":
            if client := self._model._connection.client_if_connected:
                self.upscaler = client.default_upscaler

    @property
    def target_extent(self):
        return self._model.document.extent * self.factor

    @property
    def params(self):
        return UpscaleParams(
            upscaler=self.upscaler,
            factor=self.factor,
            use_diffusion=self.use_diffusion,
            strength=self.strength,
            target_extent=self.target_extent,
            seed=self._model.seed if self._model.fixed_seed else workflow.generate_seed(),
        )


class LiveWorkspace(QObject, ObservableProperties):
    is_active = Property(False, setter="toggle")
    is_recording = Property(False, setter="toggle_record")
    strength = Property(0.3, persist=True)
    has_result = Property(False)

    is_active_changed = pyqtSignal(bool)
    is_recording_changed = pyqtSignal(bool)
    strength_changed = pyqtSignal(float)
    seed_changed = pyqtSignal(int)
    has_result_changed = pyqtSignal(bool)
    result_available = pyqtSignal(Image)
    modified = pyqtSignal(QObject, str)

    _model: Model
    _result: Image | None = None
    _result_bounds: Bounds | None = None
    _keyframes_folder: Path | None = None
    _keyframe_start = 0
    _keyframe_index = 0
    _keyframes: list[Path]

    def __init__(self, model: Model):
        super().__init__()
        self._model = model
        self._keyframes = []
        model.jobs.job_finished.connect(self.handle_job_finished)

    def toggle(self, active: bool):
        if self.is_active != active:
            self._is_active = active
            self.is_active_changed.emit(active)
            if active:
                self._model.generate_live()
            else:
                self.is_recording = False

    def toggle_record(self, active: bool):
        if self.is_recording != active:
            if active and not self._start_recording():
                self._model.report_error(
                    "Cannot save recorded frames, document must be saved first!"
                )
                return
            self._is_recording = active
            self.is_active = active
            self.is_recording_changed.emit(active)
            if not active:
                self._import_animation()

    def handle_job_finished(self, job: Job):
        if job.kind is JobKind.live_preview:
            if len(job.results) > 0:
                self.set_result(job.results[0], job.params.bounds)
            self.is_active = self._is_active and self._model.document.is_active
            if self.is_active:
                self._model.generate_live()

    def copy_result_to_layer(self):
        assert self.result is not None and self._result_bounds is not None
        doc = self._model.document
        doc.insert_layer(f"[Live] {self._model.prompt}", self.result, self._result_bounds)
        if settings.new_seed_after_apply:
            self._model.generate_seed()

    @property
    def result(self):
        return self._result

    def set_result(self, value: Image, bounds: Bounds):
        self._result = value
        self._result_bounds = bounds
        self.result_available.emit(value)
        self.has_result = True

        if self.is_recording:
            self._save_frame(value, bounds)

    def _start_recording(self):
        doc_filename = self._model.document.filename
        if doc_filename:
            path = Path(doc_filename)
            folder = path.parent / f"{path.with_suffix('.live-frames')}"
            folder.mkdir(exist_ok=True)
            self._keyframes_folder = folder
            while (self._keyframes_folder / f"frame-{self._keyframe_index}.webp").exists():
                self._keyframe_index += 1
            self._keyframe_start = self._keyframe_index
        else:
            self._keyframes_folder = None
        return self._keyframes_folder

    def _save_frame(self, image: Image, bounds: Bounds):
        assert self._keyframes_folder is not None
        filename = self._keyframes_folder / f"frame-{self._keyframe_index}.webp"
        self._keyframe_index += 1

        extent = self._model.document.extent
        if bounds is not None and bounds.extent != extent:
            image = Image.crop(image, bounds)
        image.save(filename)
        self._keyframes.append(filename)

    def _import_animation(self):
        self._model.document.import_animation(self._keyframes, self._keyframe_start)
        start, end = self._keyframe_start, self._keyframe_start + len(self._keyframes)
        self._model.document.active_layer.setName(f"[Rec] {start}-{end}: {self._model.prompt}")
        self._keyframes = []


class SamplingQuality(Enum):
    fast = 0
    quality = 1


class AnimationWorkspace(QObject, ObservableProperties):
    sampling_quality = Property(SamplingQuality.fast, persist=True)
    target_layer = Property(QUuid(), persist=True)
    batch_mode = Property(True, persist=True)

    sampling_quality_changed = pyqtSignal(SamplingQuality)
    target_layer_changed = pyqtSignal(QUuid)
    batch_mode_changed = pyqtSignal(bool)
    target_image_changed = pyqtSignal(Image)
    modified = pyqtSignal(QObject, str)

    _model: Model
    _keyframes_folder: Path | None = None
    _keyframes: dict[str, list[Path]]

    def __init__(self, model: Model):
        super().__init__()
        self._model = model
        self._keyframes = {}
        self.target_layer_changed.connect(self._update_target_image)
        model.document.current_time_changed.connect(self._update_target_image)
        model.jobs.job_finished.connect(self.handle_job_finished)

    def generate(self):
        if self.batch_mode:
            self.generate_batch()
        else:
            self.generate_frame()

    def generate_frame(self):
        self._model.clear_error()
        eventloop.run(_report_errors(self._model, self._generate_frame()))

    async def _generate_frame(self):
        strength = self._model.strength
        live = self.sampling_quality is SamplingQuality.fast
        seed = self._model.seed if self._model.fixed_seed else workflow.generate_seed()
        bounds = Bounds(0, 0, *self._model.document.extent)
        image = self._model._get_current_image(bounds) if strength < 1.0 else None
        control = [c.get_image(bounds) for c in self._model.control]
        cond = Conditioning(self._model.prompt, self._model.negative_prompt, control)
        job = await self._model._generate(
            bounds, cond, strength, image, None, seed, count=1, is_live=live
        )
        job[0].kind = JobKind.animation_frame
        job[0].params.frame = (self._model.document.current_time, 0, 0)

    def generate_batch(self):
        doc = self._model.document
        if self._model.strength < 1.0 and not doc.active_layer.animated():
            self._model.report_error("The active layer does not contain an animation.")
            return

        if doc.filename:
            path = Path(doc.filename)
            folder = path.parent / f"{path.with_suffix('.animation')}"
            folder.mkdir(exist_ok=True)
            self._keyframes_folder = folder
        else:
            self._model.report_error("Document must be saved before generating an animation.")
            return

        self._model.clear_error()
        eventloop.run(_report_errors(self._model, self._generate_batch()))

    async def _generate_batch(self):
        doc = self._model.document
        layer = doc.active_layer
        start_frame, end_frame = doc.playback_time_range
        extent = doc.extent
        bounds = Bounds(0, 0, *extent)
        strength = self._model.strength
        live = self.sampling_quality is SamplingQuality.fast
        seed = self._model.seed if self._model.fixed_seed else workflow.generate_seed()
        animation_id = str(uuid.uuid4())

        for frame in range(start_frame, end_frame + 1):
            if layer.hasKeyframeAtTime(frame) or strength == 1.0:
                image = None
                if strength < 1.0:
                    pixels = layer.pixelDataAtTime(0, 0, extent.width, extent.height, frame)
                    image = Image(QImage(pixels, extent.width, extent.height, QImage.Format_ARGB32))

                control = [c.get_image(bounds) for c in self._model.control]
                cond = Conditioning(self._model.prompt, self._model.negative_prompt, control)
                job = await self._model._generate(
                    bounds, cond, strength, image, None, seed, count=1, is_live=live
                )
                job[0].kind = JobKind.animation_batch
                job[0].params.frame = (frame, start_frame, end_frame)
                job[0].params.animation_id = animation_id

    def handle_job_finished(self, job: Job):
        if job.kind is JobKind.animation_batch:
            assert self._keyframes_folder is not None
            frame, _, end = job.params.frame
            keyframes = self._keyframes.setdefault(job.params.animation_id, [])
            if len(job.results) > 0:
                image = job.results[0]
                filename = self._keyframes_folder / f"frame-{frame}.png"
                image.save(filename)
                keyframes.append(filename)
                self.target_image_changed.emit(image)
            elif len(keyframes) > 0:
                # Execution was cached because image content is the same as previous frame
                keyframes.append(keyframes[-1])
            if frame == end:
                self._import_animation(job)

        elif job.kind is JobKind.animation_frame:
            if len(job.results) > 0:
                doc = self._model.document
                if job.params.frame[0] != doc.current_time:
                    self._model.report_error("Generated frame does not match current time")
                    return
                if layer := self._model.layers.find(self.target_layer):
                    image = job.results[0]
                    doc.set_layer_content(layer, image, job.params.bounds, make_visible=False)
                    self.target_image_changed.emit(image)
                else:
                    self._model.report_error("Target layer not found")

    def _import_animation(self, job: Job):
        doc = self._model.document
        keyframes = self._keyframes.pop(job.params.animation_id)
        _, start, end = job.params.frame
        doc.import_animation(keyframes, start)
        doc.active_layer.setName(f"[Generated] {start}-{end}: {job.params.prompt}")
        self.target_layer = doc.active_layer.uniqueId()

    def _update_target_image(self):
        if self.batch_mode:
            return
        if layer := self._model.layers.find(self.target_layer):
            bounds = Bounds(0, 0, *self._model.document.extent)
            image = self._model.document.get_layer_image(layer, bounds)
            self.target_image_changed.emit(image)


def get_selection_modifiers(inpaint_mode: InpaintMode) -> dict[str, Any]:
    grow = settings.selection_grow / 100
    feather = settings.selection_feather / 100
    padding = settings.selection_padding / 100
    invert = False

    if inpaint_mode is InpaintMode.remove_object:
        # avoid leaving any border pixels of the object to be removed within the
        # area where the mask is 1.0, it will confuse inpainting models
        feather = min(feather, grow * 0.5)

    if inpaint_mode is InpaintMode.replace_background:
        # only minimal grow/feather as there is often no desired transition between
        # forground object and background (to be replaced by something else entirely)
        grow = min(grow, 0.01)
        feather = min(feather, 0.01)
        invert = True

    return dict(grow=grow, feather=feather, padding=padding, invert=invert)


async def _report_errors(parent: Model, coro):
    try:
        return await coro
    except NetworkError as e:
        parent.report_error(f"{util.log_error(e)} [url={e.url}, code={e.code}]")
    except Exception as e:
        parent.report_error(util.log_error(e))


def _save_job_result(model: Model, job: Job | None, index: int):
    assert job is not None, "Cannot save result, invalid job id"
    assert len(job.results) > index, "Cannot save result, invalid result index"
    assert model.document.filename, "Cannot save result, document is not saved"
    timestamp = job.timestamp.strftime("%Y%m%d-%H%M%S")
    prompt = util.sanitize_prompt(job.params.prompt)
    path = Path(model.document.filename)
    path = path.parent / f"{path.stem}-generated-{timestamp}-{index}-{prompt}.webp"
    path = util.find_unused_path(path)
    base_image = model._get_current_image(Bounds(0, 0, *model.document.extent))
    result_image = job.results[index]
    base_image.draw_image(result_image, job.params.bounds.offset)
    base_image.save(path)
