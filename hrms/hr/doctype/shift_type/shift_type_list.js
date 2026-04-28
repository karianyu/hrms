frappe.listview_settings["Shift Type"] = {

	onload: function (listview) {
		hrms.add_shift_tools_button_to_list(listview);

		listview.page.add_action_item(__("Mark Attendance - All"), () => {
			frappe.call({
				method: "hrms.hr.doctype.shift_type.shift_type.process_auto_attendance_for_all_shifts",
				freeze: true,
			});
		});
	},	
};
