import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/admin/presentation/admin_widgets.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart' show Paged;
import 'package:employee_connect/features/payroll/data/import_file_picker.dart';
import 'package:employee_connect/features/payroll/data/payroll_delivery.dart';
import 'package:employee_connect/features/payroll/data/payroll_repository.dart';
import 'package:employee_connect/features/payroll/domain/pay_items_models.dart';
import 'package:employee_connect/features/payroll/domain/payroll_models.dart';
import 'package:employee_connect/features/payroll/presentation/payroll_providers.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

Map<String, dynamic> payslipJson({
  String id = 'p1',
  String status = 'published',
  int revision = 1,
  bool admin = false,
  int version = 3,
  bool hold = false,
  String userId = 'u1',
  Map<String, dynamic>? revisionInfo,
  List<String> errors = const [],
}) =>
    {
      'id': id,
      'period': '2026-10',
      'revision': revision,
      'status': status,
      'published_at': '2026-10-31T09:00:00Z',
      'pay_date': '2026-10-31',
      'company': {'name': 'Aksan Capital Advisory Private Limited', 'address': 'Chennai'},
      'employee': {
        'employee_id': 'EMP001',
        'full_name': 'Asha Rao',
        'department': 'Advisory',
        'designation': 'Analyst',
        'join_date': '2024-06-01',
        'pan': 'XXXXX1234X',
        'bank_account': 'XXXXXX1234',
        'payment_mode': 'bank_transfer',
      },
      'days': {'basis_days': 31.0, 'employed_days': 31.0, 'lop_days': 1.5, 'paid_days': 29.5},
      'earnings': [
        {'code': 'BASIC', 'label': 'Basic', 'monthly': '20000.00', 'amount': '19032.00'},
        {'code': 'BONUS', 'label': 'Diwali bonus', 'monthly': null, 'amount': '5000.00'},
      ],
      'deductions': [
        {'code': 'PF', 'label': 'Provident Fund', 'amount': '1800.00'},
        {'code': 'TDS', 'label': 'Income Tax (TDS)', 'amount': '1200.00'},
      ],
      'employer_contributions': null,
      'totals': {
        'gross_earnings': '38500.00',
        'total_deductions': '3200.00',
        'net_pay': '35300.00',
        'net_pay_words': 'Rupees Thirty-Five Thousand Three Hundred Only',
      },
      'revision_info': revisionInfo,
      if (admin) ...{
        'run_id': 'r1',
        'user': {'id': userId, 'employee_id': 'EMP001', 'full_name': 'Asha Rao'},
        'hold': hold,
        'version': version,
        'lop': {
          'auto': 1.5,
          'override': null,
          'override_reason': null,
          'breakdown': {'unpaid_leave': 1.0, 'absent': 0.0, 'half_day_attendance': 0.5},
        },
        'inputs': {'tds': '1200.00', 'pt_override': null, 'adjustments': <Object?>[], 'note': null},
        'errors': errors,
        'warnings': <Object?>[],
      },
    };

Map<String, dynamic> runJson({String status = 'draft'}) => {
      'id': 'r1',
      'period': '2026-10',
      'status': status,
      'pay_date': '2026-10-31',
      'attendance_cutoff': '2026-10-25',
      'counts': {'employees': 2, 'draft': 2, 'processed': 0, 'published': 0, 'errors': 1, 'warnings': 1},
      'totals': {
        'gross_earnings': '1850000.00',
        'total_deductions': '160000.00',
        'net_pay': '1690000.00',
      },
    };

Map<String, dynamic> rowJson({String id = 'p1', List<String> errors = const [], String? net = '35300.00'}) => {
      'id': id,
      'user': {'id': 'u1', 'employee_id': 'EMP001', 'full_name': 'Asha Rao'},
      'revision': 1,
      'status': 'draft',
      'hold': false,
      'paid_days': 29.5,
      'lop_days': 1.5,
      'gross_earnings': net,
      'total_deductions': net == null ? null : '3200.00',
      'net_pay': net,
      'errors': errors,
      'warnings': <String>[],
    };

class FakePicker implements PayrollImportPicker {
  FakePicker(this.file);
  PickedWorkbook? file;

  @override
  Future<PickedWorkbook?> pickWorkbook() async => file;
}

class FakeDelivery implements PayrollFileDelivery {
  final List<PayrollFile> files = [];
  DeliveryOutcome outcome = DeliveryOutcome.done;

  @override
  Future<DeliveryOutcome> deliver(BuildContext context, PayrollFile file) async {
    files.add(file);
    return outcome;
  }
}

class FakeRepo extends PayrollRepository {
  FakeRepo() : super(Dio());

  @override
  Future<Map<String, dynamic>> dashboard(String period) async => dashboardJson;

  @override
  Future<Map<String, dynamic>> variance(String period) async => varianceJson;

  @override
  Future<PayrollFile> report(String kind, String period, {String? currentPassword}) async {
    reportCalls.add(kind);
    return PayrollFile(Uint8List(0), 'payroll-$kind-$period.xlsx');
  }

  // statutory
  List<StatutoryMonth> duesList = [];
  ApiException? duesError;
  ApiException? fileError;
  ApiException? challanError;
  int challanWrongPasswords = 0;
  final List<String> statutoryFileCalls = [];
  final List<String?> statutoryFilePasswords = [];
  int fileWrongPasswords = 0;
  final List<Map<String, Object?>> challanCalls = [];

  @override
  Future<List<StatutoryMonth>> statutoryDues(String period, {int months = 1}) async {
    if (duesError != null) throw duesError!;
    return duesList;
  }

  @override
  Future<PayrollFile> statutoryFile(String kind, String period, {String? currentPassword}) async {
    statutoryFileCalls.add('$kind:$period');
    statutoryFilePasswords.add(currentPassword);
    if (fileWrongPasswords > 0) {
      fileWrongPasswords--;
      throw const ApiException(code: 'REAUTH_FAILED', message: 'no', status: 403);
    }
    if (fileError != null) throw fileError!;
    return PayrollFile(Uint8List.fromList([1]), 'statutory-$kind-$period.xlsx');
  }

  @override
  Future<void> setChallan({
    required String type,
    required String period,
    required bool paid,
    required String currentPassword,
    String? paidOn,
    String? reference,
    String? amount,
  }) async {
    challanCalls.add({
      'type': type,
      'period': period,
      'paid': paid,
      'paid_on': paidOn,
      'reference': reference,
      'amount': amount,
      'pw': currentPassword,
    });
    if (challanWrongPasswords > 0) {
      challanWrongPasswords--;
      throw const ApiException(code: 'REAUTH_FAILED', message: 'no', status: 403);
    }
    if (challanError != null) throw challanError!;
  }

  // data
  List<PayslipListItem> mine = [];
  Payslip? myDetail;
  ApiException? listError;
  PayrollRun run0 = PayrollRun.fromJson(runJson());
  List<PayrollRunItem> runList = [];
  List<RunPayslipRow> rows = [RunPayslipRow.fromJson(rowJson())];
  Payslip? adminPayslip;
  List<SalaryStructure> structureList = [];
  PayrollDetails detailsMasked = const PayrollDetails(
    userId: 'u1',
    designation: 'Analyst',
    paymentMode: 'bank_transfer',
    includeInPayroll: true,
    revealed: false,
    bankAccount: 'XXXXXX1234',
    pan: 'XXXXX1234X',
  );
  PayrollDetails detailsRevealed = const PayrollDetails(
    userId: 'u1',
    designation: 'Analyst',
    paymentMode: 'bank_transfer',
    includeInPayroll: true,
    revealed: true,
    bankAccount: '123456789012',
    pan: 'ABCDE1234F',
  );
  Map<String, Object?> settingsMap = {};
  Map<String, dynamic> dashboardJson = {
    'period': '2026-11',
    'run': null,
    'headcount': 2,
    'held': 0,
    'errors': 0,
    'warnings': 0,
    'totals': {'gross': '100000.00', 'deductions': '20000.00', 'net': '80000.00', 'employer_cost': '104000.00'},
    'departments': [
      {'department': 'Sales', 'employees': 2, 'gross': '100000.00', 'net': '80000.00', 'employer_cost': '104000.00'},
    ],
    'trend': [
      {'period': '2026-11', 'employees': 2, 'gross': '100000.00', 'net': '80000.00', 'employer_cost': '104000.00'},
    ],
    'joiners': <Object?>[],
    'leavers': <Object?>[],
    'calendar': {
      'cutoff_day': 25,
      'process_by': '2026-11-28',
      'pay_date': '2026-12-01',
      'alerts': ['RUN_NOT_CREATED'],
    },
  };
  Map<String, dynamic> varianceJson = {'period': '2026-11', 'previous': '2026-10', 'items': <Object?>[]};
  final List<String> reportCalls = [];
  List<PayrollEmployee> employeeList = [];

  // behaviour switches
  ApiException? processError;
  ApiException? saveError;
  int wrongPasswords = 0;

  // recorded calls
  final calls = <String>[];
  final List<Map<String, Object?>> savedPatches = [];
  final List<String> publishPasswords = [];
  final List<Map<String, Object?>> savedSettings = [];
  final List<String?> settingsPasswords = [];
  final List<Map<String, Object?>> savedDetails = [];
  final List<Map<String, Object?>> createdStructures = [];
  final List<({bool includeBank, String? pw})> registerCalls = [];
  String? revisedReason;
  bool revealCalled = false;

  @override
  Future<List<PayslipListItem>> myPayslips(int year) async {
    calls.add('myPayslips:$year');
    if (listError != null) throw listError!;
    return mine;
  }

  @override
  Future<Payslip> myPayslip(String id) async {
    if (listError != null) throw listError!;
    return myDetail!;
  }

  @override
  Future<PayrollFile> myPayslipPdf(String id) async {
    calls.add('myPdf:$id');
    return PayrollFile(Uint8List.fromList([37, 80, 68, 70]), 'payslip-EMP001-2026-10.pdf');
  }

  @override
  Future<List<PayrollRunItem>> runs(int year) async => runList;

  @override
  Future<PayrollRun> run(String id) async => run0;

  @override
  Future<PayrollRun> createRun(String period, {String? payDate}) async {
    calls.add('createRun:$period:$payDate');
    return run0;
  }

  @override
  Future<Paged<RunPayslipRow>> runPayslips(RunPayslipQuery q, {int page = 1}) async =>
      Paged(items: rows, page: 1, total: rows.length, pageSize: 20);

  @override
  Future<PayrollRun> processRun(String id) async {
    calls.add('process');
    if (processError != null) throw processError!;
    return run0;
  }

  @override
  Future<PayrollRun> refreshRun(String id) async {
    calls.add('refresh');
    return run0;
  }

  @override
  Future<PayrollRun> publishRun(String id, {required String currentPassword}) async {
    publishPasswords.add(currentPassword);
    if (wrongPasswords > 0) {
      wrongPasswords--;
      throw const ApiException(code: 'REAUTH_FAILED', message: 'no', status: 403);
    }
    return run0;
  }

  @override
  Future<PayrollFile> register(
    String runId,
    String period, {
    bool includeBank = false,
    String? currentPassword,
  }) async {
    registerCalls.add((includeBank: includeBank, pw: currentPassword));
    return PayrollFile(Uint8List.fromList([1]), 'payroll-register-2026-10.xlsx');
  }

  @override
  Future<Payslip> payslip(String id) async => adminPayslip!;

  @override
  Future<Payslip> savePayslip(String id, Map<String, Object?> patch) async {
    savedPatches.add(patch);
    if (saveError != null) throw saveError!;
    return adminPayslip!;
  }

  @override
  Future<Payslip> revise(String runId, String userId, String reason) async {
    revisedReason = reason;
    return Payslip.fromJson(payslipJson(id: 'p2', status: 'draft', revision: 2, admin: true));
  }

  @override
  Future<List<SalaryStructure>> structures(String userId) async => structureList;

  @override
  Future<SalaryStructure> createStructure(String userId, StructureInput input) async {
    createdStructures.add(input.toJson());
    return SalaryStructure.fromJson({
      'id': 's9',
      'user_id': userId,
      'effective_from': input.effectiveFrom,
      'basic': input.basic,
      'hra': input.hra,
      'monthly_gross': input.monthlyGross,
    });
  }

  @override
  Future<PayrollDetails> details(String userId, {bool reveal = false}) async {
    if (reveal) revealCalled = true;
    return reveal ? detailsRevealed : detailsMasked;
  }

  @override
  Future<PayrollDetails> saveDetails(
    String userId,
    Map<String, Object?> changes, {
    required String currentPassword,
  }) async {
    savedDetails.add({...changes, '_pw': currentPassword});
    return detailsMasked;
  }

  @override
  Future<Map<String, Object?>> settings() async => settingsMap;

  @override
  Future<Map<String, Object?>> saveSettings(Map<String, Object?> changes, {String? currentPassword}) async {
    savedSettings.add(changes);
    settingsPasswords.add(currentPassword);
    return settingsMap;
  }

  @override
  Future<Paged<PayrollEmployee>> employees(PayrollEmployeeQuery q, {int page = 1}) async =>
      Paged(items: employeeList, page: 1, total: employeeList.length, pageSize: 20);

  // ---- pay items (section 17) ---------------------------------------------------

  List<OneTimeItem> itemList = [];
  ApiException? itemsError;
  ApiException? writeError;
  int pwRejects = 0;
  final List<Map<String, Object?>> itemCalls = [];
  final List<ItemQuery> itemQueries = [];
  ImportReport? dryReport;
  ApiException? importError;
  ImportCommit commitResult = const ImportCommit(batchId: 'b2', period: '2026-10', created: 2);
  final List<Map<String, Object?>> importCalls = [];
  List<ImportBatch> batchList = [];
  final List<String> batchCancels = [];
  List<Loan> loanList = [];
  Loan? loanDetail;
  final List<Map<String, Object?>> loanCalls = [];
  List<MyLoan> myLoanList = [];
  MyLoan? myLoanDetail;
  ApiException? myLoansError;
  List<Arrears> arrearsList = [];
  Arrears? recalcResult;
  final List<Map<String, Object?>> arrearsCalls = [];
  List<Encashment> encashmentList = [];
  EncashmentQuote? quote;
  ApiException? quoteError;
  List<LeaveTypeRef> encashableTypes = const [LeaveTypeRef(id: 'lt1', code: 'EL', name: 'Earned Leave')];
  final List<Map<String, Object?>> encashCalls = [];

  void _writeCheck(String? pw) {
    if (pw != null && pwRejects > 0) {
      pwRejects--;
      throw const ApiException(code: 'REAUTH_FAILED', message: 'no', status: 403);
    }
    if (writeError != null) throw writeError!;
  }

  @override
  Future<Paged<OneTimeItem>> oneTimeItems(ItemQuery q, {int page = 1}) async {
    itemQueries.add(q);
    if (itemsError != null) throw itemsError!;
    return Paged(items: itemList, page: 1, total: itemList.length, pageSize: 20);
  }

  @override
  Future<OneTimeItem> createItem({
    required String userId,
    required String period,
    required String code,
    required String label,
    required String amount,
  }) async {
    _writeCheck(null);
    itemCalls.add({'op': 'create', 'user': userId, 'period': period, 'code': code, 'label': label, 'amount': amount});
    return itemList.isEmpty ? OneTimeItem.fromJson({'id': 'x', 'version': 1}) : itemList.first;
  }

  @override
  Future<OneTimeItem> updateItem(
    String id, {
    required int expectedVersion,
    String? period,
    String? label,
    String? amount,
  }) async {
    _writeCheck(null);
    itemCalls.add({'op': 'update', 'id': id, 'v': expectedVersion, 'period': period, 'label': label, 'amount': amount});
    return itemList.first;
  }

  @override
  Future<OneTimeItem> cancelItem(String id, String reason) async {
    _writeCheck(null);
    itemCalls.add({'op': 'cancel', 'id': id, 'reason': reason});
    return itemList.first;
  }

  @override
  Future<PayrollFile> itemsTemplate() async {
    calls.add('template');
    return PayrollFile(Uint8List.fromList([1]), 'payroll-items-template.xlsx');
  }

  @override
  Future<ImportReport> importDryRun({
    required List<int> bytes,
    required String filename,
    required String period,
  }) async {
    importCalls.add({'op': 'dry', 'name': filename, 'period': period, 'len': bytes.length});
    if (importError != null) throw importError!;
    return dryReport!;
  }

  @override
  Future<ImportCommit> importCommit({
    required List<int> bytes,
    required String filename,
    required String period,
    required String fileSha256,
    required String currentPassword,
  }) async {
    importCalls.add({'op': 'commit', 'period': period, 'sha': fileSha256, 'pw': currentPassword});
    _writeCheck(currentPassword);
    return commitResult;
  }

  @override
  Future<List<ImportBatch>> importBatches({String? period}) async => batchList;

  @override
  Future<BatchCancelResult> cancelBatch(String id, String reason) async {
    batchCancels.add('$id:$reason');
    return const BatchCancelResult(cancelled: 3, notCancelled: 1);
  }

  @override
  Future<Paged<Loan>> loans(LoanQuery q, {int page = 1}) async =>
      Paged(items: loanList, page: 1, total: loanList.length, pageSize: 20);

  @override
  Future<Loan> loan(String id) async => loanDetail!;

  @override
  Future<Loan> createLoan({
    required String userId,
    required String kind,
    required String principal,
    required String emi,
    required String startPeriod,
    required String reason,
    required String currentPassword,
  }) async {
    loanCalls.add({
      'op': 'create',
      'user': userId,
      'kind': kind,
      'principal': principal,
      'emi': emi,
      'start': startPeriod,
      'reason': reason,
      'pw': currentPassword,
    });
    _writeCheck(currentPassword);
    return loanDetail ?? Loan.fromJson({'id': 'l9', 'reference': 'L-0009', 'version': 1});
  }

  @override
  Future<Loan> skipInstallment(
    String loanId, {
    required String installmentId,
    required String reason,
    required int expectedVersion,
  }) async {
    loanCalls.add({'op': 'skip', 'inst': installmentId, 'reason': reason, 'v': expectedVersion});
    _writeCheck(null);
    return loanDetail!;
  }

  @override
  Future<Loan> rescheduleLoan(
    String loanId, {
    required String emi,
    required String fromPeriod,
    required String reason,
    required int expectedVersion,
    required String currentPassword,
  }) async {
    loanCalls.add({
      'op': 'reschedule',
      'emi': emi,
      'from': fromPeriod,
      'reason': reason,
      'v': expectedVersion,
      'pw': currentPassword,
    });
    _writeCheck(currentPassword);
    return loanDetail!;
  }

  @override
  Future<Loan> forecloseLoan(
    String loanId, {
    required String mode,
    required String reason,
    required int expectedVersion,
    required String currentPassword,
    String? period,
    String? paidOn,
    String? reference,
  }) async {
    loanCalls.add({
      'op': 'foreclose',
      'mode': mode,
      'period': period,
      'paid_on': paidOn,
      'reference': reference,
      'reason': reason,
      'v': expectedVersion,
      'pw': currentPassword,
    });
    _writeCheck(currentPassword);
    return loanDetail!;
  }

  @override
  Future<Loan> cancelLoan(
    String loanId, {
    required String reason,
    required int expectedVersion,
    required String currentPassword,
  }) async {
    loanCalls.add({'op': 'cancel', 'reason': reason, 'v': expectedVersion, 'pw': currentPassword});
    _writeCheck(currentPassword);
    return loanDetail!;
  }

  @override
  Future<List<MyLoan>> myLoans() async {
    if (myLoansError != null) throw myLoansError!;
    return myLoanList;
  }

  @override
  Future<MyLoan> myLoan(String id) async => myLoanDetail!;

  @override
  Future<Paged<Arrears>> arrears(ArrearsQuery q, {int page = 1}) async =>
      Paged(items: arrearsList, page: 1, total: arrearsList.length, pageSize: 20);

  @override
  Future<Arrears?> recalculateArrears(String userId) async {
    arrearsCalls.add({'op': 'recalc', 'user': userId});
    _writeCheck(null);
    return recalcResult;
  }

  @override
  Future<Arrears> acceptArrears(
    String id, {
    required String period,
    required int expectedVersion,
    required String currentPassword,
  }) async {
    arrearsCalls.add({'op': 'accept', 'id': id, 'period': period, 'v': expectedVersion, 'pw': currentPassword});
    _writeCheck(currentPassword);
    return arrearsList.first;
  }

  @override
  Future<Arrears> dismissArrears(String id, String reason) async {
    arrearsCalls.add({'op': 'dismiss', 'id': id, 'reason': reason});
    _writeCheck(null);
    return arrearsList.first;
  }

  @override
  Future<Paged<Encashment>> encashments(EncashmentQuery q, {int page = 1}) async =>
      Paged(items: encashmentList, page: 1, total: encashmentList.length, pageSize: 20);

  @override
  Future<EncashmentQuote> encashmentQuote(
    String userId, {
    required String leaveTypeId,
    required int year,
    required String period,
    double? days,
  }) async {
    encashCalls.add({'op': 'quote', 'user': userId, 'type': leaveTypeId, 'year': year, 'period': period, 'days': days});
    if (quoteError != null) throw quoteError!;
    return quote!;
  }

  @override
  Future<Encashment> createEncashment({
    required String userId,
    required String leaveTypeId,
    required int year,
    required double days,
    required String period,
    required String reason,
    required String currentPassword,
  }) async {
    encashCalls.add({
      'op': 'create',
      'user': userId,
      'type': leaveTypeId,
      'year': year,
      'days': days,
      'period': period,
      'reason': reason,
      'pw': currentPassword,
    });
    _writeCheck(currentPassword);
    return encashmentList.first;
  }

  @override
  Future<Encashment> cancelEncashment(String id, {String? reason}) async {
    encashCalls.add({'op': 'cancel', 'id': id});
    _writeCheck(null);
    return encashmentList.first;
  }

  @override
  Future<List<LeaveTypeRef>> encashableLeaveTypes() async => encashableTypes;
}

/// Pumps [child] with the fake repo, permissions and a fixed clock.
Future<void> pumpPayroll(
  WidgetTester tester,
  Widget child, {
  required FakeRepo repo,
  List<String> perms = const [],
  String? selfId = 'me',
  FakeDelivery? delivery,
  PayrollImportPicker? picker,
  Size size = const Size(800, 2400),
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        payrollRepositoryProvider.overrideWithValue(repo),
        adminPermissionsProvider.overrideWithValue(perms),
        adminSelfIdProvider.overrideWithValue(selfId),
        payrollDeliveryProvider.overrideWithValue(delivery ?? FakeDelivery()),
        if (picker != null) payrollImportPickerProvider.overrideWithValue(picker),
        payrollClockProvider.overrideWithValue(() => DateTime(2026, 10, 25)),
      ],
      child: MaterialApp(home: child),
    ),
  );
  await tester.pumpAndSettle();
}
