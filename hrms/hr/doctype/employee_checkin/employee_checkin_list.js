frappe.listview_settings["Employee Checkin"] = {
	add_fields: ["offshift"],
	get_indicator: function (doc) {
		if (doc.offshift) {
			return [__("Off-Shift"), "yellow", "offshift,=,1"];
		}
	},
	onload: function (listview) {
		listview.page.add_action_item(__("Fetch Shifts"), () => {
			const checkins = listview.get_checked_items().map((checkin) => checkin.name);
			frappe.call({
				method: "hrms.hr.doctype.employee_checkin.employee_checkin.bulk_fetch_shift",
				freeze: true,
				args: {
					checkins,
				},
			});
		});

		listview.page.add_action_item(__("Fetch Shifts - All"), () => {
			const checkins = listview.get_checked_items().map((checkin) => checkin.name);
			frappe.call({
				method: "hrms.hr.doctype.employee_checkin.employee_checkin.bulk_shift_monthly_fetch",
				freeze: true,
				args: {
					checkins,
				},
			});
		});
		
		listview.page.add_action_item(__("Create Attendance"), () => {
			const checkins = listview.get_checked_items().map((checkin) => checkin.name);
			frappe.call({
				method: "hrms.hr.doctype.employee_checkin.employee_checkin.create_draft_attendance_for_offshift_checkins",
				freeze: true,
				args: {
					checkins,
				},
			});
		});
	},
};
