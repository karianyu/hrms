// Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Shift Assignment Tool", {
	setup(frm) {
		hrms.setup_employee_filter_group(frm);
	},

	refresh(frm) {
		frm.page.clear_indicator();
		frm.disable_save();
		frm.trigger("set_primary_action");
		frm.trigger("get_employees");

		hrms.handle_realtime_bulk_action_notification(
			frm,
			"completed_bulk_shift_assignment",
			"Shift Assignment",
		);
		hrms.handle_realtime_bulk_action_notification(
			frm,
			"completed_bulk_shift_schedule_assignment",
			"Shift Schedule Assignment",
		);
		hrms.handle_realtime_bulk_action_notification(
			frm,
			"completed_bulk_shift_request_processing",
			"Shift Request",
		);

		// Listen for the auto-roster engine completion event
		frappe.realtime.on("completed_auto_roster_assignment", (data) => {
			frm.events.handle_auto_roster_complete(frm, data);
		});
	},

	action(frm) {
		frm.trigger("set_primary_action");
		frm.trigger("get_employees");
	},

	company(frm) {
		frm.trigger("get_employees");
	},

	shift_type(frm) {
		frm.trigger("get_employees");
	},

	status(frm) {
		frm.trigger("get_employees");
	},

	start_date(frm) {
		if (frm.doc.start_date > frm.doc.end_date) frm.set_value("end_date", null);
		frm.trigger("get_employees");
	},

	end_date(frm) {
		if (frm.doc.end_date < frm.doc.start_date) frm.set_value("start_date", null);
		frm.trigger("get_employees");
	},

	shift_type_filter(frm) {
		frm.trigger("get_employees");
	},

	shift_schedule(frm) {
		frm.trigger("get_employees");
	},

	approver(frm) {
		frm.trigger("get_employees");
	},

	from_date(frm) {
		if (frm.doc.from_date > frm.doc.to_date) frm.set_value("to_date", null);
		frm.trigger("get_employees");
	},

	to_date(frm) {
		if (frm.doc.to_date < frm.doc.from_date) frm.set_value("from_date", null);
		frm.trigger("get_employees");
	},

	branch(frm) {
		frm.trigger("get_employees");
	},

	department(frm) {
		frm.trigger("get_employees");
	},

	designation(frm) {
		frm.trigger("get_employees");
	},

	grade(frm) {
		frm.trigger("get_employees");
	},

	employment_type(frm) {
		frm.trigger("get_employees");
	},

	set_primary_action(frm) {
		const select_rows_section_head = document
			.querySelector('[data-fieldname="select_rows_section"]')
			.querySelector(".section-head");
		select_rows_section_head.textContent = __("Select Employees");
		frm.clear_custom_buttons();
		frm.page.clear_primary_action();

		if (frm.doc.action === "Assign Shift") {
			frm.page.set_primary_action(__("Assign Shift"), () => {
				frm.trigger("bulk_assign");
			});

			// Auto-Assign Roster sits as a secondary action alongside the
			// primary "Assign Shift" button — only shown when prerequisites
			// (company, shift_type, start_date, end_date) are all present.
			if (frm.doc.company && frm.doc.shift_type && frm.doc.start_date && frm.doc.end_date) {
				frm.add_custom_button(__("Auto-Assign Roster"), () => {
					frm.events.show_auto_roster_dialog(frm);
				});
			}
		} else if (frm.doc.action === "Assign Shift Schedule") {
			frm.page.set_primary_action(__("Assign Shift Schedule"), () => {
				frm.trigger("bulk_assign");
			});
		} else {
			frm.page.add_inner_button(
				__("Approve"),
				() => {
					frm.events.process_shift_requests(frm, "Approved");
				},
				__("Process Requests"),
			);
			frm.page.add_inner_button(
				__("Reject"),
				() => {
					frm.events.process_shift_requests(frm, "Rejected");
				},
				__("Process Requests"),
			);
			frm.page.set_inner_btn_group_as_primary(__("Process Requests"));
			frm.page.clear_menu();
			select_rows_section_head.textContent = __("Select Shift Requests");
		}
	},

	get_employees(frm) {
		if (
			(frm.doc.action === "Assign Shift" && !(frm.doc.shift_type && frm.doc.start_date)) ||
			(frm.doc.action === "Assign Shift Schedule" &&
				!(frm.doc.shift_schedule && frm.doc.start_date))
		)
			return frm.events.render_employees_datatable(frm, []);

		frm.call({
			method: "get_employees",
			args: {
				advanced_filters: frm.advanced_filters || [],
			},
			doc: frm.doc,
		}).then((r) => frm.events.render_employees_datatable(frm, r.message));
	},

	render_employees_datatable(frm, employees) {
		let columns = undefined;
		let no_data_message = undefined;
		if (frm.doc.action === "Assign Shift") {
			columns = frm.events.get_assign_shift_datatable_columns();
			no_data_message = __(
				frm.doc.shift_type && frm.doc.start_date
					? "There are no employees without Shift Assignments for these dates based on the given filters."
					: "Please select Shift Type and assignment date(s).",
			);
		} else if (frm.doc.action === "Assign Shift Schedule") {
			columns = frm.events.get_assign_shift_datatable_columns();
			no_data_message = __(
				frm.doc.shift_schedule && frm.doc.start_date
					? "There are no employees without active overlapping Shift Schedule Assignments based on the given filters."
					: "Please select Shift Schedule and assignment date(s).",
			);
		} else {
			columns = frm.events.get_process_shift_requests_datatable_columns();
			no_data_message = "There are no open Shift Requests based on the given filters.";
		}
		hrms.render_employees_datatable(frm, columns, employees, no_data_message);
	},

	get_assign_shift_datatable_columns() {
		return [
			{
				name: "employee",
				id: "employee",
				content: __("Employee"),
			},
			{
				name: "employee_name",
				id: "employee_name",
				content: __("Employee Name"),
			},
			{
				name: "branch",
				id: "branch",
				content: __("Branch"),
			},
			{
				name: "department",
				id: "department",
				content: __("Department"),
			},
			{
				name: "default_shift",
				id: "default_shift",
				content: __("Default Shift"),
			},
		].map((x) => ({
			...x,
			editable: false,
			focusable: false,
			dropdown: false,
			align: "left",
		}));
	},

	get_process_shift_requests_datatable_columns() {
		return [
			{
				name: "shift_request",
				id: "shift_request",
				content: __("Shift Request"),
			},
			{
				name: "employee",
				id: "employee_name",
				content: __("Employee"),
			},
			{
				name: "shift_type",
				id: "shift_type",
				content: __("Shift Type"),
			},
			{
				name: "from_date",
				id: "from_date",
				content: __("From Date"),
			},
			{
				name: "to_date",
				id: "to_date",
				content: __("To Date"),
			},
		].map((x) => ({
			...x,
			editable: false,
			focusable: false,
			dropdown: false,
			align: "left",
		}));
	},

	bulk_assign(frm, employees) {
		const rows = frm.employees_datatable.datamanager.data;
		const selected_employees = [];
		const checked_row_indexes = frm.employees_datatable.rowmanager.getCheckedRows();
		checked_row_indexes.forEach((idx) => {
			selected_employees.push(rows[idx].employee);
		});

		hrms.validate_mandatory_fields(frm, selected_employees);
		frappe.confirm(
			__("{0} to {1} employee(s)?", [__(frm.doc.action), selected_employees.length]),
			() => {
				frm.call({
					method: "bulk_assign",
					doc: frm.doc,
					args: {
						employees: selected_employees,
					},
					freeze: true,
					freeze_message: __("Assigning..."),
				});
			},
		);
	},

	process_shift_requests(frm, status) {
		const rows = frm.employees_datatable.datamanager.data;
		const selected_requests = [];
		const checked_row_indexes = frm.employees_datatable.rowmanager.getCheckedRows();
		checked_row_indexes.forEach((idx) => {
			selected_requests.push({
				shift_request: rows[idx].name,
				employee: rows[idx].employee,
			});
		});

		hrms.validate_mandatory_fields(frm, selected_requests, "Shift Requests");
		frappe.confirm(
			__("Process {0} Shift Request(s) as <b>{1}</b>?", [selected_requests.length, status]),
			() => {
				frm.events.bulk_process_shift_requests(frm, selected_requests, status);
			},
		);
	},

	bulk_process_shift_requests(frm, shift_requests, status) {
		frm.call({
			method: "bulk_process_shift_requests",
			doc: frm.doc,
			args: {
				shift_requests: shift_requests,
				status: status,
			},
			freeze: true,
			freeze_message: __("Processing Requests"),
		});
	},

	// -------------------------------------------------------------------------
	// Auto-Roster: entry point
	// -------------------------------------------------------------------------

	show_auto_roster_dialog(frm) {
		// Collect all employees currently visible in the datatable (respects all
		// active filters the user has already applied), then open the config dialog.
		const all_rows = frm.employees_datatable?.datamanager?.data || [];
		if (!all_rows.length) {
			frappe.msgprint({
				title: __("No Employees"),
				message: __("No employees are visible in the table. Adjust your filters first."),
				indicator: "orange",
			});
			return;
		}

		// Determine which shift types to pre-populate.  If the form has a
		// shifts_for_auto_generation child table use that; otherwise fall back
		// to the single shift_type field.
		const raw_shifts = frm.doc.shifts_for_auto_generation
			? frm.doc.shifts_for_auto_generation
			: (frm.doc.shift_type ? [{ shift_type: frm.doc.shift_type }] : []);

		// const default_shift_rows = raw_shifts.map((r) => ({
		// 	shift_type:   r.shift_type || r,
		// 	min_coverage: r.min_coverage || 1,
		// }));
		const default_shift_rows = raw_shifts.map((r) => ({
			shift_type:           r.shift_type || r,
			min_coverage:         r.min_coverage || 1,
			max_coverage:         r.max_coverage !== undefined ? r.max_coverage : null,
			weekend_max_coverage: r.weekend_max_coverage !== undefined ? r.weekend_max_coverage : null,
			sunday_max_coverage:  r.sunday_max_coverage  !== undefined ? r.sunday_max_coverage  : null,
		}));

		// ── Build the per-shift coverage table HTML ──────────────────────────
		// Rendered as a simple editable HTML table inside an HTML field so we
		// don't depend on a child doctype.  Values are read back via DOM queries
		// when the user clicks Preview or Confirm.

		// ── Build the per-shift coverage table HTML (NOW WITH MAX COLUMN) ─────
		const _shift_table_html = (rows) => {
			if (!rows.length) {
				return `<p style="color:var(--text-muted); font-size:12px;">
					${__("No shift types found. Add them using the Shift Type field above.")}</p>`;
			}
			let html = `
				<table id="shift-coverage-table"
					style="width:100%; border-collapse:collapse; font-size:12px; margin-top:4px;">
					<thead>
						<tr>
							<th style="text-align:left; padding:5px 8px; border-bottom:1px solid var(--border-color);
								font-weight:500; color:var(--text-muted); width:30%;">${__("Shift Type")}</th>
							<th style="text-align:center; padding:5px 8px; border-bottom:1px solid var(--border-color);
								font-weight:500; color:var(--text-muted);">${__("Min / Day")}<br><span style="font-size:10px; font-weight:400;">(Mon–Fri)</span></th>
							<th style="text-align:center; padding:5px 8px; border-bottom:1px solid var(--border-color);
								font-weight:500; color:var(--text-muted);">${__("Max / Day")}<br><span style="font-size:10px; font-weight:400;">(Saturday)</span></th>
							<th style="text-align:center; padding:5px 8px; border-bottom:1px solid var(--border-color);
								font-weight:500; color:var(--text-muted);">${__("Max / Day")}<br><span style="font-size:10px; font-weight:400;">(Sunday)</span></th>
							<th style="text-align:center; padding:5px 8px; border-bottom:1px solid var(--border-color);
								font-weight:500; color:var(--text-muted);">
								${__("Max / Day")}
								<br><span style="font-size:10px; font-weight:400; color:var(--text-muted);">(blank = no limit)</span>
							</th>
						</tr>
					</thead>
					<tbody>`;
			rows.forEach(({ shift_type, min_coverage, max_coverage = null, weekend_max_coverage = null, sunday_max_coverage = null }) => {
				const max_val     = max_coverage !== null ? max_coverage : "";
				const wknd_val    = weekend_max_coverage !== null ? weekend_max_coverage : "";
				const sun_val     = sunday_max_coverage  !== null ? sunday_max_coverage  : "";
				const input_style = `width:52px; text-align:center; border:1px solid var(--border-color);
						border-radius:4px; padding:2px 6px; background:var(--control-bg);`;
				html += `
					<tr data-shift="${frappe.utils.escape_html(shift_type)}">
						<td style="padding:6px 8px; border-bottom:1px solid var(--border-color);">
							${frappe.utils.escape_html(shift_type)}
						</td>
						<td style="padding:6px 8px; border-bottom:1px solid var(--border-color); text-align:center;">
							<input type="number" min="1" value="${min_coverage}"
								data-shift="${frappe.utils.escape_html(shift_type)}" data-type="min"
								style="${input_style}" />
						</td>
						<td style="padding:6px 8px; border-bottom:1px solid var(--border-color); text-align:center;">
							<input type="number" min="0" value="${wknd_val}"
								data-shift="${frappe.utils.escape_html(shift_type)}" data-type="weekend_max"
								placeholder="same"
								style="${input_style}" />
						</td>
						<td style="padding:6px 8px; border-bottom:1px solid var(--border-color); text-align:center;">
							<input type="number" min="0" value="${sun_val}"
								data-shift="${frappe.utils.escape_html(shift_type)}" data-type="sunday_max"
								placeholder="same"
								style="${input_style}" />
						</td>
						<td style="padding:6px 8px; border-bottom:1px solid var(--border-color); text-align:center;">
							<input type="number" min="0" value="${max_val}"
								data-shift="${frappe.utils.escape_html(shift_type)}" data-type="max"
								placeholder="unlimited"
								style="${input_style}" />
						</td>
					</tr>`;
			});
			html += `</tbody></table>`;
			return html;
		};

		// Helper: read current values from the DOM table (NOW READS BOTH MIN + MAX)
		const _read_shift_rows = (dialog) => {
			const rows = [];
			dialog.$wrapper.find("#shift-coverage-table tbody tr").each(function () {
				const shift_type    = $(this).data("shift");
				const min_input     = $(this).find("input[data-type='min']");
				const wknd_input    = $(this).find("input[data-type='weekend_max']");
				const sun_input     = $(this).find("input[data-type='sunday_max']");
				const max_input     = $(this).find("input[data-type='max']");

				const min_coverage  = parseInt(min_input.val(), 10) || 1;

				let weekend_max = parseInt(wknd_input.val(), 10);
				if (isNaN(weekend_max) || weekend_max < 0) weekend_max = null;

				let sunday_max  = parseInt(sun_input.val(), 10);
				if (isNaN(sunday_max)  || sunday_max  < 0) sunday_max  = null;

				let max_coverage = parseInt(max_input.val(), 10);
				if (isNaN(max_coverage) || max_coverage <= 0) max_coverage = null;

				if (shift_type) rows.push({ shift_type, min_coverage, weekend_max_coverage: weekend_max, sunday_max_coverage: sunday_max, max_coverage });
			});
			return rows;
		};

		const dialog = new frappe.ui.Dialog({
			title: __("Auto-Assign Roster"),
			fields: [
				{
					fieldtype: "Section Break",
					label: __("Scope"),
					description: __(
						"The roster will cover all {0} employees currently shown in the table, " +
						"from {1} to {2}.",
						[all_rows.length, frm.doc.start_date, frm.doc.end_date],
					),
				},
				{
					fieldtype: "Section Break",
					label: __("Shift Coverage Requirements"),
					description: __(
						"Set the minimum number of employees required per shift per day. " +
						"The engine will guarantee this floor and distribute any additional " +
						"available employees across shifts to maximise utilisation.",
					),
				},
				{
					fieldtype: "HTML",
					fieldname: "shift_coverage_table",
					label: "",
					options: _shift_table_html(default_shift_rows),
				},
				{
					fieldtype: "Section Break",
					label: __("Preview"),
				},
				{
					fieldtype: "HTML",
					fieldname: "roster_preview_html",
					label: "",
				},
			],
			primary_action_label: __("Confirm & Assign"),
			primary_action() {
				if (!dialog._roster_payload) {
					frappe.msgprint({
						title: __("Preview Required"),
						message: __("Please click 'Preview Roster' before confirming."),
						indicator: "orange",
					});
					return;
				}
				dialog.hide();
				frm.events.confirm_auto_roster(frm, dialog._roster_payload, all_rows);
			},
		});

		// "Preview Roster" secondary button
		dialog.add_custom_action(__("Preview Roster"), () => {
			const shift_rows = _read_shift_rows(dialog);
			if (!shift_rows.length) {
				frappe.msgprint(__("Please configure at least one shift type."));
				return;
			}
			frm.events.preview_roster(frm, dialog, all_rows, shift_rows);
		});

		dialog.show();
	},

	// -------------------------------------------------------------------------
	// Auto-Roster: dry-run preview
	// -------------------------------------------------------------------------

	preview_roster(frm, dialog, all_rows, shift_rows) {
		// shift_rows: [{shift_type, min_coverage}, ...]
		const employees = all_rows.map((r) => r.employee);

		// Build an id → display label lookup so every employee ID rendered in
		// the preview shows as "Name (ID)" rather than the raw HR-EMP-XXXXX code.
		const nameMap = {};
		all_rows.forEach((r) => {
			nameMap[r.employee] = r.employee_name
				? `${r.employee_name} (${r.employee})`
				: r.employee;
		});

		const preview_field = dialog.fields_dict.roster_preview_html;
		preview_field.html(`<p style="color: var(--text-muted);">${__("Loading preview…")}</p>`);

		frm.call({
			method: "preview_roster",
			doc: frm.doc,
			args: {
				employees,
				shift_types: shift_rows,
			},
			freeze: false,
		}).then((r) => {
			if (!r.message) return;
			const result = r.message;
			// Stash payload — server still needs IDs, names are only for display
			dialog._roster_payload = { employees, shift_types: shift_rows };
			frm.events.render_roster_preview(dialog, result, frm, nameMap);
		});
	},

	render_roster_preview(dialog, result, frm, nameMap = {}) {
		// Resolve an employee ID to a human-readable label.
		// Falls back to the raw ID if not found in the map.
		const resolveName = (id) => nameMap[id] || id;
		const { roster, workload, uncovered, skipped_leave, forced_rest = {}, rest_blocked = {}, hours_capped = {}, rotation_block = {}, shift_types: shiftMeta = [], rotation_cycle = [], min_coverage: coverageMap = {}, weekend_max_coverage: weekendMap = {}, sunday_max_coverage: sundayMap = {} } = result;
		const dates = Object.keys(roster).sort();
		const shift_types = dates.length
			? Object.keys(roster[dates[0]])
			: [];

		const total_slots = dates.length * shift_types.length;
		const filled_slots = dates.reduce(
			(sum, d) =>
				sum + shift_types.reduce((s2, sh) => s2 + (roster[d][sh] || []).length, 0),
			0,
		);
		const gap_days = Object.keys(uncovered).length;

		// ── Shift type legend (timing metadata + rotation order) ────────────
		let html = "";
		if (shiftMeta.length) {
			// Build a rotation-order label map so each pill shows its position
			const cyclePos = {};
			rotation_cycle.forEach((name, i) => { cyclePos[name] = i + 1; });
			const totalInCycle = rotation_cycle.length;

			html += `<div style="margin-bottom:6px; font-size:11px; font-weight:500; color:var(--text-muted);">
						${__("Rotation cycle")}:
						<span style="font-weight:400;">${rotation_cycle.join(" → ")}</span>
					</div>`;
			html += `<div style="display:flex; flex-wrap:wrap; gap:6px; margin-bottom:14px;">`;
			shiftMeta.forEach(({ shift_type, duration_h, is_overnight, start_h, end_h }) => {
				const startStr  = frm.events._hh_mm(start_h);
				const endStr    = frm.events._hh_mm(end_h);
				const overnight = is_overnight ? ` <span style="font-size:10px; opacity:.7;">${__("overnight")}</span>` : "";
				const pos       = cyclePos[shift_type];
				const posLabel  = pos ? `<span style="font-size:10px; color:var(--primary); margin-left:3px;">⟳${pos}/${totalInCycle}</span>` : "";
			const minLabel = coverageMap[shift_type]
				? (() => {
					const wknd = weekendMap[shift_type];
					const sun  = sundayMap[shift_type];
					let extra = "";
					if (wknd != null) extra += ` <span style="font-size:10px; color:var(--text-muted);">Sat max:${wknd}</span>`;
					if (sun  != null) extra += ` <span style="font-size:10px; color:var(--text-muted);">Sun:${sun}</span>`;
					return `<span style="font-size:10px; color:var(--text-muted); margin-left:4px;">min ${coverageMap[shift_type]}${extra}</span>`;
				})()
				: "";
				html += `<span style="display:inline-flex; align-items:center; gap:5px; font-size:11px;
							background:var(--control-bg); border:0.5px solid var(--border-color);
							border-radius:4px; padding:3px 8px;">
							<strong>${shift_type}</strong>${posLabel}${minLabel}
							<span style="color:var(--text-muted);">${startStr}–${endStr} · ${duration_h}h${overnight}</span>
						</span>`;
			});
			html += `</div>`;
		}

		// ── Summary metrics ──────────────────────────────────────────────────
		const total_hours = workload.reduce((s, w) => s + (w.total_hours || 0), 0);
		// const targetLabel  = target_days_per_week ? `${__("Target")}: ${target_days_per_week}d/wk` : "";
		html += `
			<div style="display:grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 16px;">
				${frm.events._metric_card(__("Days"), dates.length)}
				${frm.events._metric_card(__("Slots filled"), filled_slots)}
				${frm.events._metric_card(__("Total hours"), Math.round(total_hours))}
				${frm.events._metric_card(__("Coverage gaps"), gap_days, gap_days > 0 ? "var(--alert-text-warning)" : "")}
			</div>`;

		// ── Workload balance bar chart (hours-weighted) ───────────────────────
		const max_hours = Math.max(...workload.map((w) => w.total_hours || 0), 1);
		html += `<p style="font-size:11px; font-weight:500; color:var(--text-muted); margin:0 0 6px;">${__("Workload balance (hours)")}</p>`;
		html += `<div style="margin-bottom:16px;">`;
		workload.forEach(({ employee, assigned_days, total_hours: emp_hours }) => {
			const pct      = Math.round(((emp_hours || 0) / max_hours) * 100);
			const dispName = resolveName(employee);
			html += `
				<div style="display:flex; align-items:center; gap:8px; margin-bottom:5px; font-size:12px;">
					<div style="width:140px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
						color:var(--text-color); flex-shrink:0;" title="${dispName}">${dispName}</div>
					<div style="flex:1; height:6px; background:var(--border-color); border-radius:3px; overflow:hidden;">
						<div style="width:${pct}%; height:100%; background:var(--primary); border-radius:3px;"></div>
					</div>
					<div style="width:60px; text-align:right; color:var(--text-muted); font-size:11px; flex-shrink:0;">${emp_hours || 0}h · ${assigned_days}d</div>
				</div>`;
		});
		html += `</div>`;

		// ── Coverage gap warnings ─────────────────────────────────────────────
		if (gap_days > 0) {
			html += `<div style="background:var(--alert-bg-warning); border:1px solid var(--alert-border-warning);
						border-radius:4px; padding:8px 12px; margin-bottom:16px; font-size:12px;">
						<strong>${__("Coverage gaps detected")}</strong> —
						${__("{0} day(s) have fewer employees than the minimum required.", [gap_days])}
						${__("Consider adding more employees or reducing the minimum coverage.")}</div>`;
		}

		// ── On-leave notices ─────────────────────────────────────────────────
		if (skipped_leave?.length) {
			html += `<p style="font-size:12px; color:var(--text-muted);">
						${__("Employees skipped on at least one day due to leave:")}
						<strong>${skipped_leave.map(resolveName).join(", ")}</strong></p>`;
		}

		// ── Forced-rest notices (consecutive-day cap) ────────────────────────
		const forced_rest_entries = Object.entries(forced_rest);
		if (forced_rest_entries.length) {
			html += `<div style="background:var(--alert-bg-info); border:1px solid var(--alert-border-info);
						border-radius:4px; padding:8px 12px; margin-bottom:12px; font-size:12px;">
						<strong>${__("Mandatory rest days applied")}</strong> —
						${__("The following employees hit the consecutive-day cap and were given a rest day:")}<br>`;
			forced_rest_entries.forEach(([emp, dates]) => {
				html += `<span style="display:inline-block; margin-top:4px;">
							<strong>${resolveName(emp)}</strong>: ${dates.map(d => frappe.datetime.str_to_user(d)).join(", ")}
						</span><br>`;
			});
			html += `</div>`;
		}

		// ── Rest-gap blocks ────────────────────────────────────────────────────
		const rest_blocked_entries = Object.entries(rest_blocked);
		if (rest_blocked_entries.length) {
			html += `<div style="background:var(--alert-bg-info); border:1px solid var(--alert-border-info);
						border-radius:4px; padding:8px 12px; margin-bottom:12px; font-size:12px;">
						<strong>${__("Rest-gap blocks applied")}</strong> —
						${__("These employees were skipped on dates where they had fewer than {0}h of rest after their previous shift:", [result.min_rest_hours || 11])}<br>`;
			rest_blocked_entries.forEach(([emp, dates]) => {
				html += `<span style="display:inline-block; margin-top:4px;">
							<strong>${resolveName(emp)}</strong>: ${dates.map(d => frappe.datetime.str_to_user(d)).join(", ")}
						</span><br>`;
			});
			html += `</div>`;
		}

		// ── Weekly-hours-cap blocks ────────────────────────────────────────────
		const hours_capped_entries = Object.entries(hours_capped);
		if (hours_capped_entries.length) {
			html += `<div style="background:var(--alert-bg-warning); border:1px solid var(--alert-border-warning);
						border-radius:4px; padding:8px 12px; margin-bottom:12px; font-size:12px;">
						<strong>${__("Weekly hours cap applied")}</strong> —
						${__("These employees were skipped on dates where assigning them would exceed the {0}h/week limit:", [result.max_weekly_hours || 48])}<br>`;
			hours_capped_entries.forEach(([emp, dates]) => {
				html += `<span style="display:inline-block; margin-top:4px;">
							<strong>${resolveName(emp)}</strong>: ${dates.map(d => frappe.datetime.str_to_user(d)).join(", ")}
						</span><br>`;
			});
			html += `</div>`;
		}

		// ── Rotation-block notices ────────────────────────────────────────────
		const rotation_block_entries = Object.entries(rotation_block);
		if (rotation_block_entries.length) {
			html += `<div style="background:var(--alert-bg-info); border:1px solid var(--alert-border-info);
						border-radius:4px; padding:8px 12px; margin-bottom:12px; font-size:12px;">
						<strong>${__("Rotation blocks applied")}</strong> —
						${__("These employees were skipped on dates to enforce the rotation cycle (backward phase jumps or same-shift streak exceeded {0} days):", [result.max_same_shift_days || 4])}<br>`;
			rotation_block_entries.forEach(([emp, dates]) => {
				html += `<span style="display:inline-block; margin-top:4px;">
							<strong>${resolveName(emp)}</strong>: ${dates.map(d => frappe.datetime.str_to_user(d)).join(", ")}
						</span><br>`;
			});
			html += `</div>`;
		}

		// ── Roster table (first 7 days to keep the dialog compact) ───────────
		const preview_days = dates;
		html += `<p style="font-size:11px; font-weight:500; color:var(--text-muted); margin:0 0 6px;">
					${__("Roster preview")}
					${dates.length > 7 ? `<span style="font-weight:400;">(${__("first 7 days shown")})</span>` : ""}
				</p>`;
		html += `<div style="overflow-x:auto;">
					<table style="width:100%; border-collapse:collapse; font-size:12px;">
						<thead>
							<tr>
								<th style="text-align:left; padding:5px 8px; border-bottom:1px solid var(--border-color);
									font-weight:500; color:var(--text-muted);">${__("Date")}</th>`;
		shift_types.forEach((sh) => {
			const minReq = coverageMap[sh];
			const minBadge = minReq
				? ` <span style="font-weight:400; font-size:10px; color:var(--text-muted);">(min ${minReq})</span>`
				: "";
			html += `<th style="text-align:left; padding:5px 8px; border-bottom:1px solid var(--border-color);
							font-weight:500; color:var(--text-muted);">${sh}${minBadge}</th>`;
		});
		html += `		</tr>
					</thead>
					<tbody>`;
		preview_days.forEach((day) => {
			const is_gap = !!uncovered[day];
			html += `<tr style="${is_gap ? "background:var(--alert-bg-warning);" : ""}">
						<td style="padding:5px 8px; border-bottom:1px solid var(--border-color);
							white-space:nowrap; color:var(--text-muted); font-size:11px;">
							${frappe.datetime.str_to_user(day)}</td>`;
			// Per-shift colour palette — rotates through 4 distinct hues
			const SHIFT_COLORS = [
				{ bg: "var(--blue-50,#e6f1fb)",   text: "var(--blue-800,#0c447c)"  },
				{ bg: "var(--green-50,#eaf3de)",  text: "var(--green-800,#27500a)" },
				{ bg: "var(--amber-50,#faeeda)",  text: "var(--amber-800,#633806)" },
				{ bg: "var(--coral-50,#faece7)",  text: "var(--coral-800,#712b13)" },
			];
			const shiftColorMap = {};
			shift_types.forEach((s, i) => { shiftColorMap[s] = SHIFT_COLORS[i % SHIFT_COLORS.length]; });

			shift_types.forEach((sh) => {
				const emps     = roster[day][sh] || [];
				const shortage = (uncovered[day] || {})[sh];
				const col      = shiftColorMap[sh] || SHIFT_COLORS[0];
				html += `<td style="padding:5px 8px; border-bottom:1px solid var(--border-color);">`;
				html += emps
					.map(
						(e) =>
							`<span style="display:inline-block; background:${col.bg}; color:${col.text};
								padding:1px 6px; border-radius:4px; margin:1px; font-size:11px;"
								title="${e}">${resolveName(e)}</span>`,
					)
					.join("");
				if (shortage) {
					html += `<span style="font-size:10px; color:var(--alert-text-warning);
								margin-left:4px;">−${shortage} needed</span>`;
				}
				if (!emps.length && !shortage) {
					html += `<span style="color:var(--text-muted);">—</span>`;
				}
				html += `</td>`;
			});
			html += `</tr>`;
		});
		html += `</tbody></table></div>`;

		dialog.fields_dict.roster_preview_html.html(html);
	},

	_hh_mm(decimal_hours) {
		// Convert a decimal hour value (e.g. 6.5) to "06:30" display string.
		const h = Math.floor(decimal_hours % 24);
		const m = Math.round((decimal_hours % 1) * 60);
		return String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0");
	},

	_metric_card(label, value, color = "") {
		return `
			<div style="background:var(--control-bg); border-radius:6px; padding:10px 12px;">
				<div style="font-size:20px; font-weight:500; color:${color || "var(--text-color)"};">${value}</div>
				<div style="font-size:11px; color:var(--text-muted); margin-top:2px;">${label}</div>
			</div>`;
	},

	// -------------------------------------------------------------------------
	// Auto-Roster: final confirmation → server write
	// -------------------------------------------------------------------------

	confirm_auto_roster(frm, payload, all_rows) {
		const { employees, shift_types } = payload;
		// shift_types is [{shift_type, min_coverage}, ...]
		const shift_names    = shift_types.map((r) => r.shift_type);
		const total_min      = shift_types.reduce((s, r) => s + (r.min_coverage || 1), 0);

		frappe.confirm(
			__(
				"Assign roster for <b>{0}</b> employee(s) across <b>{1}</b> shift type(s) " +
				"(min. {2} slot(s)/day total) from <b>{3}</b> to <b>{4}</b>?",
				[employees.length, shift_names.length, total_min, frm.doc.start_date, frm.doc.end_date],
			),
			() => {
				frm.call({
					method: "auto_assign_roster",
					doc: frm.doc,
					args: {
						employees,
						shift_types,   // structured format — server parses min_coverage from here
						dry_run: false,
					},
					freeze: true,
					freeze_message: __("Building roster…"),
				});
				// Result is delivered via the realtime event registered in refresh()
			},
		);
	},

	// -------------------------------------------------------------------------
	// Auto-Roster: realtime completion handler
	// -------------------------------------------------------------------------

	handle_auto_roster_complete(frm, data) {
		const { success = [], failure = [], uncovered = {}, forced_rest = {}, rest_blocked = {}, hours_capped = {}, rotation_block = {} } = data;
		const gap_count = Object.keys(uncovered).length;

		// Split created assignments into Active vs Inactive buckets
		const active_created   = success.filter(s => s.status !== "Inactive");
		const inactive_created = success.filter(s => s.status === "Inactive");

		let indicator = "green";
		let title = __("Roster Assigned");
		let lines = [
			`<b>${active_created.length}</b> ${__("shift assignment(s) created (Active).")}`,
		];

		if (inactive_created.length) {
			indicator = "orange";
			lines.push(
				`<b>${inactive_created.length}</b> ${__("assignment(s) created as <b>Inactive</b> due to conflicts — review and activate manually.")}`,
			);
		}

		if (failure.length) {
			indicator = "red";
			lines.push(
				`<b>${failure.length}</b> ${__("assignment(s) could not be created even as Inactive — see Error Log for details.")}`,
			);
		}
		if (gap_count) {
			indicator = indicator === "green" ? "orange" : indicator;
			lines.push(
				`<b>${gap_count}</b> ${__("day(s) had coverage gaps (not enough available employees).")}`,
			);
		}

		const streak_rest_count = Object.values(forced_rest).reduce((s, dates) => s + dates.length, 0);
		if (streak_rest_count) {
			lines.push(
				`<b>${streak_rest_count}</b> ${__("mandatory rest day(s) enforced (consecutive-day cap).")}`,
			);
		}

		const gap_rest_count = Object.values(rest_blocked).reduce((s, dates) => s + dates.length, 0);
		if (gap_rest_count) {
			lines.push(
				`<b>${gap_rest_count}</b> ${__("assignment(s) skipped — insufficient rest after previous shift.")}`,
			);
		}

		const budget_count = Object.values(hours_capped).reduce((s, dates) => s + dates.length, 0);
		if (budget_count) {
			lines.push(
				`<b>${budget_count}</b> ${__("assignment(s) skipped — weekly hours budget would be exceeded.")}`,
			);
		}

		const rotation_count = Object.values(rotation_block).reduce((s, dates) => s + dates.length, 0);
		if (rotation_count) {
			lines.push(
				`<b>${rotation_count}</b> ${__("assignment(s) deferred to enforce shift rotation (no backward phase jumps, same-shift streak respected).")}`,
			);
		}

		frappe.msgprint({
			title,
			message: lines.join("<br>"),
			indicator,
		});

		// Refresh the datatable so already-assigned employees disappear
		frm.trigger("get_employees");
	},
});