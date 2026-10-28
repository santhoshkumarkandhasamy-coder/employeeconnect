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
from .payroll import EmployeePayrollDetails, PayrollRun, Payslip, SalaryStructure
from .security import AuditLog, DeviceToken, PasswordResetRequest, RefreshToken, Setting
from .tasks import Task
from .users import EmployeeProfile, File, User

__all__ = [
    "Base", "Role", "Permission", "RolePermission", "Department", "Shift", "User", "File", "EmployeeProfile",
    "RefreshToken", "DeviceToken", "PasswordResetRequest", "AuditLog", "Setting",
    "Holiday", "Attendance", "LateRecord", "LeaveType", "LeaveBalance", "LeaveRequest", "PermissionRequest", "ClientLocation", "OnDutyRequest", "Task",
    "Company", "License", "SalaryStructure", "EmployeePayrollDetails", "PayrollRun", "Payslip", "FoodEnrollment", "Announcement", "AnnouncementDepartment", "AnnouncementRead",
]
