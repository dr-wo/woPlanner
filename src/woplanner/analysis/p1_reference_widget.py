"""Manual, snapshot-scoped leader reference; never a standard model publisher."""
from __future__ import annotations

import copy
import json
import math

from PySide6 import QtCore, QtWidgets

from .worker import P1ReferenceProcessService


class P1ReferenceWidget(QtWidgets.QGroupBox):
    def __init__(self, window):
        super().__init__("Leader Pace Estimation", window)
        self.window = window
        self.worker = P1ReferenceProcessService(window.service.data_root)
        self.result = None
        self._generation = 0
        self._request_generation = None
        self._standard_summary = None
        self._session_identity = None
        self._observed_lap = 0
        self._restore = None
        layout = QtWidgets.QVBoxLayout(self)
        description = QtWidgets.QLabel(
            "Estimate the leader's pace from selected completed laps, ignoring the gap behind while retaining "
            "other clean-lap checks. This independent reference changes team strategy inputs only when you "
            "choose Apply; the standard MC model remains unchanged."
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        controls = QtWidgets.QHBoxLayout()
        self.driver = QtWidgets.QComboBox()
        self.driver.setMinimumWidth(120)
        self.first_lap = QtWidgets.QSpinBox()
        self.last_lap = QtWidgets.QSpinBox()
        for spin in (self.first_lap, self.last_lap):
            spin.setRange(1, 1)
        controls.addWidget(QtWidgets.QLabel("P1"))
        controls.addWidget(self.driver)
        controls.addWidget(QtWidgets.QLabel("Lap range"))
        controls.addWidget(self.first_lap)
        controls.addWidget(QtWidgets.QLabel("to"))
        controls.addWidget(self.last_lap)
        self.calculate = QtWidgets.QPushButton("Calculate Reference")
        controls.addWidget(self.calculate)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.sector_traffic = QtWidgets.QCheckBox("Use sector timing to assess traffic ahead")
        self.sector_traffic.setToolTip(
            "Required for this reference. Uses observed sector crossings to check the gap ahead. "
            "The standard MC traffic setting is unchanged."
        )
        layout.addWidget(self.sector_traffic)
        self.acknowledge = QtWidgets.QCheckBox(
            "Confirm selected laps are suitable for pace estimation"
        )
        self.acknowledge.setToolTip(
            "Selected laps should be free from wheel-to-wheel battles, defensive driving or significant "
            "tactical interference. A consistently close P2 alone does not make laps unsuitable."
        )
        layout.addWidget(self.acknowledge)
        suitability = QtWidgets.QLabel(
            "Selected laps should be free from wheel-to-wheel battles, defensive driving or significant tactical interference."
        )
        suitability.setWordWrap(True)
        layout.addWidget(suitability)
        self.status = QtWidgets.QLabel("Waiting for completed race laps.")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.comparison = QtWidgets.QTableWidget(0, 5)
        self.comparison.setHorizontalHeaderLabels(
            ["Parameter / scope", "Compound", "Standard MC median", "P1 reference median [P10, P90]", "Use in strategy"]
        )
        self.comparison.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.comparison.setMaximumHeight(185)
        self.comparison.setMinimumHeight(160)
        self.comparison.setVisible(False)
        layout.addWidget(self.comparison)
        actions = QtWidgets.QHBoxLayout()
        self.apply = QtWidgets.QPushButton("Apply Reference to Team")
        self.restore = QtWidgets.QPushButton("Restore Previous Inputs")
        self.details = QtWidgets.QPushButton("Diagnostics…")
        self.details.setEnabled(False)
        self.apply.setEnabled(False)
        self.restore.setEnabled(False)
        actions.addWidget(self.apply)
        actions.addWidget(self.restore)
        actions.addWidget(self.details)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addStretch(1)
        self.calculate.clicked.connect(self._calculate)
        self.apply.clicked.connect(self._apply)
        self.restore.clicked.connect(self._restore_previous)
        self.details.clicked.connect(self._show_diagnostics)
        self.acknowledge.toggled.connect(self.invalidate)
        self.sector_traffic.toggled.connect(self.invalidate)
        self.driver.currentIndexChanged.connect(self.invalidate)
        self.first_lap.valueChanged.connect(self.invalidate)
        self.last_lap.valueChanged.connect(self.invalidate)
        for control in (
            window.sample_count, window.quick_threshold, window.min_clean_laps,
            window.exclude_stint_start_laps, window.exclude_stint_end_laps,
            window.min_gap_ahead, window.enforce_compound_order,
            *window.clean_compound_checks.values(),
        ):
            signal = (control.toggled if isinstance(control, QtWidgets.QCheckBox)
                      else control.currentIndexChanged if isinstance(control, QtWidgets.QComboBox)
                      else control.valueChanged)
            signal.connect(self.invalidate)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self._poll)
        self._refresh_actions()

    def invalidate(self, *_args):
        self._generation += 1
        self.result = None
        self.comparison.setVisible(False)
        if self._observed_lap:
            self.status.setText("Selection updated. Calculate Reference to compare results.")
        self._refresh_team_availability()
        self._refresh_actions()

    def supported_teams(self):
        teams = set()
        if self.result is not None and self.result.status == "updated" and self.result.applicable_parameters:
            teams.add(self.result.team)
        if self._restore is not None:
            teams.add(self._restore[0])
        return teams

    def _refresh_team_availability(self):
        w = self.window
        if not hasattr(w, "team_scope_grid") or w._current_model_summary is None:
            return
        selected = w._selected_team
        w._refresh_strategy_teams(w._current_model_summary)
        if selected != w._selected_team:
            w._load_strategy_inputs(w._current_model_summary)

    def consume_snapshot(self, snapshot):
        from .p1_reference import current_p1_driver

        leader = current_p1_driver(snapshot)
        observed = max((int(lap.lap_number) for lap in snapshot.completed_laps
                        if leader is not None and getattr(lap, "driver_code", None) == leader), default=0)
        info = getattr(snapshot, "session_info", {})
        identity = (self.window.year, self.window.round_number,
                    str(info.get("Key", "")), str(info.get("Path", "")), str(info.get("StartDate", "")))
        reset = self._session_identity is not None and (
            identity != self._session_identity or observed < self._observed_lap
        )
        if reset:
            self.reset_session()
        self._session_identity = identity
        self._observed_lap = observed
        if self.driver.currentData() != leader:
            self.driver.clear()
            self.driver.addItem(leader or "P1 unavailable", leader)
            self.acknowledge.setChecked(False)
            self.invalidate()
        initial = self.last_lap.maximum() == 1
        for spin in (self.first_lap, self.last_lap):
            spin.setMaximum(max(1, observed))
        if initial and observed:
            self.last_lap.setValue(observed)
        self._refresh_actions()

    def _refresh_actions(self):
        self.calculate.setEnabled(
            bool(self.driver.currentData()) and self._observed_lap > 0
            and self.first_lap.value() <= self.last_lap.value()
            and self.acknowledge.isChecked() and self.sector_traffic.isChecked()
            and not self.worker.running
        )
        result = self.result
        self.details.setEnabled(result is not None)
        supported = bool(result and result.status == "updated" and result.applicable_parameters)
        matching = (supported and self.window._selected_team == result.team
                    and self.window._current_model_summary is not None)
        self.apply.setEnabled(bool(matching))
        if supported:
            self.apply.setText(f"Apply Reference to {result.team}")
            self.apply.setToolTip(
                "Select the leader's team to apply degradation estimates for the observed compounds. "
                "Other strategy inputs and the standard MC model remain unchanged. "
                "Recalculate and apply again after reopening this window."
            )
        self.restore.setEnabled(self._restore is not None)

    def _calculate(self):
        if not self.calculate.isEnabled() or self.window._live_snapshot is None:
            return
        w = self.window
        self.invalidate()
        self._request_generation = self._generation
        self._standard_summary = copy.deepcopy(w._current_model_summary)
        options = dict(
            driver_code=self.driver.currentData(), first_lap=self.first_lap.value(),
            last_lap=self.last_lap.value(), sample_count=w.sample_count.value(),
            quick_lap_threshold=w.quick_threshold.value(), min_clean_laps=w.min_clean_laps.value(),
            allowed_compounds=w._selected_clean_compounds(),
            exclude_stint_start_laps=w.exclude_stint_start_laps.value(),
            exclude_stint_end_laps=w.exclude_stint_end_laps.value(),
            use_sector_boundary_traffic=self.sector_traffic.isChecked(),
            min_gap_ahead_seconds=w.min_gap_ahead.value(),
            enforce_compound_order=w.enforce_compound_order.isChecked(),
        )
        try:
            self.worker.submit(year=w.year, round_number=w.round_number,
                               snapshot=w._live_snapshot, options=options)
        except Exception as exc:
            self.status.setText(f"Reference calculation failed: {exc}")
            self._refresh_actions()
            return
        self.status.setText(
            f"Calculating reference for {options['driver_code']}, laps {options['first_lap']}–{options['last_lap']}…"
        )
        self._refresh_actions()
        self.timer.start()

    def _poll(self):
        try:
            result = self.worker.poll()
        except Exception as exc:
            if self._request_generation == self._generation:
                self.status.setText(f"Reference calculation failed: {exc}")
            self.timer.stop()
            self._refresh_actions()
            return
        if result is None:
            return
        self.timer.stop()
        if self._request_generation != self._generation:
            self._refresh_actions()
            return
        self.result = result
        self._refresh_team_availability()
        self._render_result()
        self._refresh_actions()

    def _render_result(self):
        result = self.result
        self.comparison.setVisible(False)
        self.status.setText(
            f"{result.driver_code} · Laps {result.first_lap}–{result.last_lap} · "
            f"{result.eligible_lap_count} eligible laps · Data through leader lap {result.leader_lap}\n"
            + ("Reference calculated. Estimates depend on fuel, track and model assumptions."
               if result.status == "updated" else "Reference does not meet sample-quality requirements."
               if result.summary is not None else result.message)
        )
        if result.summary is None:
            return
        if not result.applicable_parameters:
            self.status.setText(self.status.text() +
                "\nApply unavailable: sample quality or team support requirements are not met. See Diagnostics.")
        if result.applicable_parameters and result.team not in self.window._available_strategy_teams:
            self.status.setText(self.status.text() +
                f"\nComparison only: {result.team} is not available for strategy inputs.")
        standard = self._standard_summary
        standard_available = False
        if standard is not None:
            try:
                standard_available = int(standard.manifest.get("as_of_leader_lap", 0) or 0) <= result.leader_lap
            except (TypeError, ValueError):
                pass
        if not standard_available:
            self.status.setText(self.status.text() + "\nNo standard MC comparison is available for this race lap.")
        self.status.setText(self.status.text() +
            "\nStandard MC values are shown only where directly comparable.")
        self.comparison.setRowCount(len(result.summary.parameters))
        applicable = {(r["source_parameter"], r["compound"]) for r in result.applicable_parameters}
        def number(value):
            try:
                return f"{float(value):.4g}"
            except (TypeError, ValueError):
                return "—"
        for index, row in enumerate(result.summary.parameters):
            match = next((r for r in standard.parameters
                          if all(str(r.get(key, "")) == str(row.get(key, ""))
                                 for key in ("parameter", "compound", "team", "source_scope"))), None) if standard_available else None
            parameter = row.get("parameter")
            compatible = parameter in {"degradation", "team_degradation"}
            if parameter == "compound_delta":
                compatible = bool(row.get("reference_compound") and match
                                  and row["reference_compound"] == match.get("reference_compound"))
            if match and (not compatible or str(match.get("unit", "")) != str(row.get("unit", ""))):
                match = None
            values = (
                f"{str(row.get('parameter', '')).replace('_', ' ').capitalize()} / "
                f"{row.get('team') or str(row.get('source_scope', '')).replace('_', ' ').capitalize()}",
                str(row.get("compound") or "—"),
                number(match.get("median")) if match else "—",
                f"{number(row.get('median'))} [{number(row.get('p10'))}, {number(row.get('p90'))}]",
                "Matching team only" if (row.get("parameter"), row.get("compound")) in applicable
                and row.get("team") == result.team else "Comparison only",
            )
            for column, value in enumerate(values):
                self.comparison.setItem(index, column, QtWidgets.QTableWidgetItem(value))
        self.comparison.resizeColumnsToContents()
        self.comparison.setVisible(True)

    def _show_diagnostics(self):
        if self.result is None:
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("P1 Pace Reference — Diagnostics")
        dialog.resize(720, 520)
        layout = QtWidgets.QVBoxLayout(dialog)
        details = QtWidgets.QPlainTextEdit()
        details.setReadOnly(True)
        details.setPlainText(json.dumps({
            "selection": {"driver": self.result.driver_code, "first_lap": self.result.first_lap,
                          "last_lap": self.result.last_lap, "observed_leader_lap": self.result.leader_lap,
                          "accepted_laps": self.result.selected_lap_numbers},
            "message": self.result.message,
            "comparison": "Standard MC values require matching parameter scopes, units and reference bases. "
                          "Models using later race data are excluded.",
            "strategy_use": "Apply changes only supported degradation inputs for the leader's team. "
                            "The standard MC model remains unchanged. Recalculate and apply again after reopening.",
            "exclusions": dict(self.result.exclusion_diagnostics),
            "sampler_quality": dict(self.result.sampler_quality),
        }, indent=2, default=str))
        layout.addWidget(details)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.close)
        layout.addWidget(buttons)
        dialog.exec()

    def _apply(self):
        self._refresh_actions()
        if not self.apply.isEnabled():
            return
        w, result = self.window, self.result
        manual = w._active_manual_values()
        values = {("degradation", str(row["compound"])): float(row["median"])
                  for row in result.applicable_parameters
                  if row.get("parameter") == "degradation" and row.get("team") == result.team
                  and ("degradation", str(row["compound"])) in w._manual_overrides
                  and math.isfinite(float(row["median"]))}
        if not values:
            return
        # Reapplying keeps the original restore point, scoped to the same team.
        if self._restore is not None and self._restore[0] != w._scope_key():
            self._restore_previous()
        if self._restore is None:
            self._restore = (w._scope_key(), {}, {}, {})
        scope, previous, applied, sources = self._restore
        for coordinate, value in values.items():
            previous.setdefault(coordinate, (coordinate in manual, manual.get(coordinate)))
            sources.setdefault(coordinate[1], w._external_tyre_sources.get(coordinate[1]))
            manual[coordinate] = value
            applied[coordinate] = value
            w._external_tyre_sources[coordinate[1]] = f"P1 {result.driver_code} L{result.first_lap}–L{result.last_lap}"
        w._restore_scope_manual_sets()
        w._load_strategy_inputs(w._current_model_summary, resolved=w._resolved_tyre_inputs)
        w._save_state()
        w._append_log(f"Applied P1 pace reference {result.driver_code} L{result.first_lap}–L{result.last_lap} to {scope}: {values}")
        self.status.setText(self.status.text() + f"\nReference degradation applied to {scope} strategy inputs. Standard MC unchanged.")
        self._refresh_actions()

    def _restore_previous(self):
        if self._restore is None:
            return
        w = self.window
        scope, previous, applied, sources = self._restore
        manual = w._scope_manual_values.setdefault(scope, {})
        for coordinate, value in applied.items():
            # Keep edits made after Apply; restore only values this action still owns.
            if manual.get(coordinate) != value:
                continue
            present, old = previous[coordinate]
            if present:
                manual[coordinate] = old
            else:
                manual.pop(coordinate, None)
            if scope == w._scope_key():
                source = sources[coordinate[1]]
                if source is None:
                    w._external_tyre_sources.pop(coordinate[1], None)
                else:
                    w._external_tyre_sources[coordinate[1]] = source
        self._restore = None
        self._refresh_team_availability()
        if scope == w._scope_key():
            w._restore_scope_manual_sets()
            w._load_strategy_inputs(w._current_model_summary, resolved=w._resolved_tyre_inputs)
        w._save_state()
        self.status.setText(f"Restored previous {scope} strategy inputs; later manual edits were preserved.")
        self._refresh_actions()

    def shutdown(self):
        self.timer.stop()
        self.worker.shutdown()

    def reset_session(self):
        self._restore_previous()
        self.invalidate()
        self.shutdown()
        self._observed_lap = 0
        self._session_identity = None
        self.driver.clear()
        self.acknowledge.setChecked(False)
        self.sector_traffic.setChecked(False)
        self._refresh_actions()

    def prepare_saved_state(self, state):
        """Do not turn a snapshot-scoped alternative into an unscoped saved manual."""
        if self._restore is None:
            return
        scope, previous, applied, sources = self._restore
        manual = self.window._scope_manual_values.get(scope, {})
        saved = {(r["parameter"], r["compound"]): r["value"]
                 for r in state["scope_manual_values"].get(scope, ())}
        for coordinate, value in applied.items():
            if manual.get(coordinate) != value:
                continue
            present, old = previous[coordinate]
            if present:
                saved[coordinate] = old
            else:
                saved.pop(coordinate, None)
            if scope == self.window._scope_key():
                source = sources[coordinate[1]]
                if source is None:
                    state["external_tyre_sources"].pop(coordinate[1], None)
                else:
                    state["external_tyre_sources"][coordinate[1]] = source
        state["scope_manual_values"][scope] = [
            {"parameter": key[0], "compound": key[1], "value": value}
            for key, value in sorted(saved.items())
        ]
