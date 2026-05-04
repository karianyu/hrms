import frappe

def execute():

    # get all checkins for the whole month
    get_employee_checkins = frappe.get_all("Employee Checkins", filters={""})



    # run shift assignments for the whole month



    # run attendance for all attendances
    pass