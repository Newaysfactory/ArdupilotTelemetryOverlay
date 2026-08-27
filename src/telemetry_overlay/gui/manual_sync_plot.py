"""Interactive log-time plot for manual sync: drag to align, scroll to zoom.

Two roll-rate traces share one time axis (log time): the logged roll rate (fixed)
and the video's optical-flow roll rate, positioned by the current log delay/scale.
Left-dragging the plot shifts the video trace and the log delay live -- the drag *is*
the sync action, there is no separate "apply" step (unlike autosync's suggestion,
which is never applied on its own). The scroll wheel zooms the time axis; right-drag
rescales the roll-rate axis (a cosmetic view change, not part of the sync).
"""

from __future__ import annotations

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import Signal

from ..autosync import AutoSyncDiagnostics

#: Shown on the bare axes before any slice has been analysed, or after
#: ``clear()`` -- the equivalent of ZoomableImageView's placeholder text for
#: autosync's PNGs, adapted to a live matplotlib canvas instead of an image.
_EMPTY_PLACEHOLDER = "No analysis yet -- click 'Run auto align'."


class ManualSyncPlot(FigureCanvasQTAgg):
    """Embedded matplotlib canvas: left-drag to align, scroll to zoom, right-drag to rescale."""

    #: Emitted with the delta (seconds) to add to the log delay, once per drag step
    #: -- the caller applies it to the shared sync (e.g. via ``controller.nudge_log_delay``).
    delta_dragged = Signal(float)

    def __init__(self, parent=None) -> None:
        figure = Figure(figsize=(6, 3))
        super().__init__(figure)
        if parent is not None:
            self.setParent(parent)
        self.ax = figure.add_subplot(111)
        self._diagnostics: AutoSyncDiagnostics | None = None
        self._log_delay = 0.0
        self._scale = 1.0
        self._video_line = None
        self._drag_last_x: float | None = None
        #: Right-drag vertical rescale: pixel y at press, data-space anchor, and the
        #: y-limits to rescale from (kept fixed for the whole drag rather than
        #: recomputed each motion event, so the zoom does not compound rounding error).
        self._vscale_start_y_px: float | None = None
        self._vscale_anchor: float | None = None
        self._vscale_start_ylim: tuple[float, float] | None = None

        self.setMinimumHeight(260)
        self.mpl_connect("button_press_event", self._on_press)
        self.mpl_connect("motion_notify_event", self._on_motion)
        self.mpl_connect("button_release_event", self._on_release)
        self.mpl_connect("scroll_event", self._on_scroll)
        # Pinned explicitly rather than left to matplotlib's ambient rcParams: this
        # canvas sits inside a dark-themed window, and an ambient dark style (e.g. a
        # system matplotlibrc) would otherwise render dark text on a dark background --
        # the same failure autosync_diagnostics.png/autosync_fit.png had before their
        # colours were pinned the same way in autosync.py.
        figure.patch.set_facecolor("white")
        self._show_placeholder(_EMPTY_PLACEHOLDER)

    # ---- content -------------------------------------------------------

    def _style_axes(self) -> None:
        self.ax.set_facecolor("white")
        self.ax.tick_params(colors="black")
        self.ax.xaxis.label.set_color("black")
        self.ax.yaxis.label.set_color("black")
        self.ax.title.set_color("black")
        for spine in self.ax.spines.values():
            spine.set_color("black")

    def _show_placeholder(self, text: str) -> None:
        self.ax.clear()
        self._style_axes()
        self.ax.text(
            0.5, 0.5, text, ha="center", va="center", transform=self.ax.transAxes,
            color="black",
        )
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        for spine in self.ax.spines.values():
            spine.set_visible(False)
        self.draw_idle()

    def set_diagnostics(self, diagnostics: AutoSyncDiagnostics, log_delay: float, scale: float) -> None:
        """Load a freshly analysed slice and (re)draw both traces from scratch."""
        self._diagnostics = diagnostics
        self._log_delay = log_delay
        self._scale = scale
        self.ax.clear()
        self._style_axes()
        self.ax.plot(
            diagnostics.log_times, diagnostics.log_roll_rate, color="tab:blue", label="log roll rate"
        )
        video_times = log_delay + diagnostics.video_times * scale
        (self._video_line,) = self.ax.plot(
            video_times, diagnostics.video_roll_rate, color="tab:orange", label="video roll rate (drag to align)"
        )
        self.ax.set_xlabel("log time [s]")
        self.ax.set_ylabel("roll rate [deg/s]")
        self.ax.grid(True, alpha=0.3)
        legend = self.ax.legend(loc="upper right")
        legend.get_frame().set_facecolor("white")
        legend.get_frame().set_edgecolor("black")
        for text in legend.get_texts():
            text.set_color("black")
        self.ax.set_xlim(*self._view_around_video_trace())
        self.draw_idle()

    def update_sync(self, log_delay: float, scale: float) -> None:
        """Reposition the video trace for a sync change that did not come from a drag here."""
        if self._diagnostics is None or self._video_line is None:
            return
        self._log_delay = log_delay
        self._scale = scale
        self._video_line.set_xdata(log_delay + self._diagnostics.video_times * scale)
        self.draw_idle()

    def clear(self) -> None:
        self._diagnostics = None
        self._video_line = None
        self._show_placeholder(_EMPTY_PLACEHOLDER)

    def reset_view(self) -> None:
        """Re-fit the view to the video trace's *current* position.

        Recomputed rather than cached at analysis time: after a drag the video
        trace has moved, and resetting to a stale pre-drag range would put it
        outside the view again.
        """
        if self._diagnostics is None:
            return
        self.ax.set_xlim(*self._view_around_video_trace())
        self.draw_idle()

    def _view_around_video_trace(self) -> tuple[float, float]:
        # log_times spans the whole log (compute_manual_diagnostics resamples the full
        # attitude channel, not just the analysed slice), so fit the view to the video
        # window instead -- the whole log would be mostly empty/irrelevant.
        video_times = self._diagnostics.video_times * self._scale + self._log_delay
        lo, hi = float(video_times.min()), float(video_times.max())
        margin = (hi - lo) * 0.1 or 1.0
        return lo - margin, hi + margin

    # ---- drag to align (left button) --------------------------------------

    def _on_press(self, event) -> None:
        if self._diagnostics is None:
            return
        if event.button == 1 and event.xdata is not None:
            self._drag_last_x = event.xdata
        elif event.button == 3:
            # event.ydata can be None if the press lands outside the axes (e.g. on the
            # legend); fall back to the middle of the current view.
            lo, hi = self.ax.get_ylim()
            self._vscale_anchor = event.ydata if event.ydata is not None else (lo + hi) / 2
            self._vscale_start_y_px = event.y
            self._vscale_start_ylim = (lo, hi)

    def _on_motion(self, event) -> None:
        if self._drag_last_x is not None and event.xdata is not None and self._video_line is not None:
            delta = event.xdata - self._drag_last_x
            self._drag_last_x = event.xdata
            self._log_delay += delta
            self._video_line.set_xdata(self._diagnostics.video_times * self._scale + self._log_delay)
            self.draw_idle()
            self.delta_dragged.emit(delta)
        elif self._vscale_start_y_px is not None and event.y is not None:
            self._apply_vertical_scale(event.y)

    def _on_release(self, event) -> None:
        if event.button == 1:
            self._drag_last_x = None
        elif event.button == 3:
            self._vscale_start_y_px = None
            self._vscale_anchor = None
            self._vscale_start_ylim = None

    # ---- scroll to zoom (time axis) ---------------------------------------

    def _on_scroll(self, event) -> None:
        if event.xdata is None:
            return
        factor = 0.8 if event.step > 0 else 1.25
        lo, hi = self.ax.get_xlim()
        x = event.xdata
        self.ax.set_xlim(x - (x - lo) * factor, x + (hi - x) * factor)
        self.draw_idle()

    # ---- right-drag to rescale (roll-rate axis) ----------------------------

    def _apply_vertical_scale(self, y_px: float) -> None:
        # Pixel y increases upward in matplotlib event coordinates: dragging up
        # (positive dy) zooms in (shrinks the range), dragging down zooms out.
        dy_px = y_px - self._vscale_start_y_px
        factor = 2.0 ** (-dy_px / 150.0)
        anchor = self._vscale_anchor
        lo, hi = self._vscale_start_ylim
        self.ax.set_ylim(anchor - (anchor - lo) * factor, anchor + (hi - anchor) * factor)
        self.draw_idle()
