import 'package:employee_connect/core/api/api_exception.dart';
import 'package:employee_connect/features/food/data/food_repository.dart';
import 'package:employee_connect/features/food/domain/food_models.dart';
import 'package:employee_connect/features/leave/domain/leave_models.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  final now = DateTime(2026, 10, 21, 12, 0);

  test('parses items; null need_food is unanswered; sorted by date', () {
    final l = FoodDay.parseList({
      'items': [
        {'date': '2026-10-23', 'need_food': true, 'can_change': true, 'cutoff_at': '2026-10-22T12:30:00Z'},
        {'date': '2026-10-22', 'need_food': null, 'can_change': false, 'is_holiday': true},
        {'date': 'bad'},
      ],
    });
    expect(l.map((d) => d.date), ['2026-10-22', '2026-10-23']);
    expect(l.first.needFood, isNull);
    expect(l.first.isHoliday, isTrue);
    expect(l.last.cutoffAt, isNotNull);
  });

  test('cutoff: open, closing soon, boundary locks, holiday', () {
    final cut = DateTime(2026, 10, 21, 18, 0);
    FoodDay d(DateTime? c, {bool can = true, bool hol = false}) =>
        FoodDay(date: '2026-10-22', cutoffAt: c, canChange: can, isHoliday: hol);
    expect(cutoffStatus(d(cut), now).phase, CutoffPhase.open);
    expect(cutoffStatus(d(cut), now).label, 'Change within 6h (by 6:00 PM)');
    expect(cutoffStatus(d(cut), DateTime(2026, 10, 21, 17, 0)).phase, CutoffPhase.closingSoon);
    // now == cutoff_at is locked (change allowed only while now < cutoff_at)
    expect(cutoffStatus(d(cut), cut).phase, CutoffPhase.locked);
    // a cached can_change:true is not trusted past cutoff
    expect(cutoffStatus(d(cut), DateTime(2026, 10, 21, 19)).editable, isFalse);
    expect(cutoffStatus(d(cut, can: false), now).phase, CutoffPhase.locked);
    expect(cutoffStatus(d(null, hol: true), now).phase, CutoffPhase.holiday);
    expect(cutoffStatus(d(DateTime(2026, 10, 23, 18)), now).label, startsWith('Change before 6:00 PM'));
  });

  test('server offset corrects a wrong phone clock', () {
    final cut = DateTime(2026, 10, 21, 18, 0);
    final day = FoodDay(date: '2026-10-22', cutoffAt: cut);
    // phone says 17:00 (open) but the server is 2h ahead -> 19:00 -> locked
    final device = DateTime(2026, 10, 21, 17);
    expect(cutoffStatus(day, device).editable, isTrue);
    expect(cutoffStatus(day, serverNow(device, const Duration(hours: 2))).editable, isFalse);
  });

  test('weekTargets skips locked, holiday, and days beyond 7', () {
    final days = [
      for (var i = 0; i < 10; i++)
        FoodDay(
          date: '2026-10-${21 + i}',
          cutoffAt: DateTime(2026, 10, 20 + i, 18),
          isHoliday: i == 3,
        ),
    ];
    final t = weekTargets(days, now);
    // 21st cutoff passed (20th 18:00); 24th holiday; window is 21..27
    expect(t.map((d) => d.date), ['2026-10-22', '2026-10-23', '2026-10-25', '2026-10-26', '2026-10-27']);
  });

  test('upcomingOnly drops past cached days', () {
    final l = upcomingOnly(
      [const FoodDay(date: '2026-10-20'), const FoodDay(date: '2026-10-21')],
      now,
    );
    expect(l.length, 1);
  });

  test('groupByDepartment groups, sorts and ignores No answers', () {
    FoodEmployee e(String id, String name, String? dep, {bool need = true}) => FoodEmployee(
          person: PersonSummary(id: id, employeeId: id, fullName: name, department: dep),
          needFood: need,
        );
    final g = groupByDepartment([
      e('1', 'Zed', 'IT'),
      e('2', 'Amy', 'IT'),
      e('3', 'Bob', null),
      e('4', 'Cy', 'HR'),
      e('5', 'No', 'HR', need: false),
    ]);
    expect(g.keys, ['HR', 'IT', noDepartment]);
    expect(g['IT']!.map((x) => x.person.fullName), ['Amy', 'Zed']);
    expect(g['HR']!.length, 1);
  });

  test('daily report parses counts', () {
    final r = FoodDailyReport.fromJson({
      'date': '2026-10-21',
      'count_yes': 2,
      'count_no': 1,
      'count_unanswered': 3,
      'employees': [
        {'user': {'id': 'u', 'employee_id': 'E1', 'full_name': 'A', 'department': 'IT'}, 'need_food': true},
      ],
    });
    expect((r.countYes, r.countNo, r.countUnanswered), (2, 1, 3));
    expect(r.employees.single.person.department, 'IT');
  });

  test('error mapping and permissions', () {
    String m(String c) => foodErrorMessage(ApiException(code: c, message: 'x'));
    expect(m('FOOD_CUTOFF_PASSED'), contains('cut-off'));
    expect(m('INVALID_DATE'), contains('cannot be set'));
    expect(m('OTHER'), 'x');
    expect(canViewFoodReport(['food.view_all']), isTrue);
    expect(canViewFoodReport(['export.all']), isFalse);
    expect(canExportFood(['export.team']), isTrue);
    expect(canExportFood(const []), isFalse);
  });

  test('safeXlsxName sanitises the server file name', () {
    expect(safeXlsxName('attachment; filename="employee-connect-food-2026-10-21.xlsx"', 'd'),
        'employee-connect-food-2026-10-21.xlsx',);
    expect(safeXlsxName('attachment; filename="../../etc/passwd"', '2026-10-21'), 'passwd.xlsx');
    expect(safeXlsxName(null, '2026-10-21'), 'employee-connect-food-2026-10-21.xlsx');
  });
}
