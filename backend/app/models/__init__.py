"""SQLAlchemy models for every table. Importing this package registers all tables on Base.metadata
(Alembic env.py relies on that)."""
from .access import Permission, Role, RolePermission
from .announcements import Announcement, AnnouncementDepartment, AnnouncementRead
from .attendance import Attendance, Holiday, LateRecord
from .base import Base
from .company import Company, License
from .food import FoodEnrollment
from .leave import LeaveBalance, LeaveRequest, LeaveType, PermissionRequest
from .onduty import ClientLocation, OnDutyRequest
from .org import Department, Shift
from .payroll import (
    EmployeePayrollDetails,
    FnfSettlement,
    PayrollRun,
    Payslip,
    SalaryIncrement,
    SalaryIncrementBatch,
    SalaryStructure,
    StatutoryChallan,
    StatutoryChallanEvent,
)
from .payroll_tax import Form16Document, PayrollTaxComputation, TaxDeclaration, TaxProof, TaxYearRule
from .payroll_controls import (
    CompanyBankAccount,
    EmployeeBankChange,
    PayrollPaymentBatch,
    PayrollPaymentLine,
    PayrollPublishRequest,
    PayslipDelivery,
)
from .payroll_items import (
    LeaveEncashment,
    OvertimeDay,
    PayrollArrears,
    PayrollImportBatch,
    PayrollItemEvent,
    PayrollLoan,
    PayrollLoanInstallment,
    PayrollOneTimeItem,
    ReimbursementClaim,
)
from .security import AuditLog, DeviceToken, PasswordResetRequest, RefreshToken, Setting
from .tasks import Task
from .users import EmployeeProfile, File, User

__all__ = [
    "Base", "Role", "Permission", "RolePermission", "Department", "Shift", "User", "File", "EmployeeProfile",
    "RefreshToken", "DeviceToken", "PasswordResetRequest", "AuditLog", "Setting",
    "Holiday", "Attendance", "LateRecord", "LeaveType", "LeaveBalance", "LeaveRequest", "PermissionRequest", "ClientLocation", "OnDutyRequest", "Task",
    "Company", "License", "SalaryStructure", "SalaryIncrement", "SalaryIncrementBatch", "EmployeePayrollDetails", "FnfSettlement", "PayrollRun", "Payslip", "StatutoryChallan", "StatutoryChallanEvent",
    "PayrollItemEvent", "PayrollImportBatch", "PayrollOneTimeItem", "PayrollLoan", "PayrollLoanInstallment", "PayrollArrears", "LeaveEncashment", "ReimbursementClaim", "OvertimeDay",
    "PayrollPublishRequest", "PayslipDelivery", "CompanyBankAccount", "PayrollPaymentBatch", "PayrollPaymentLine", "EmployeeBankChange",
    "TaxYearRule", "TaxDeclaration", "TaxProof", "Form16Document", "PayrollTaxComputation",
    "FoodEnrollment", "Announcement", "AnnouncementDepartment", "AnnouncementRead",
]
