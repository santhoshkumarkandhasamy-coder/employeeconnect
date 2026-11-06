import 'package:dio/dio.dart';
import 'package:employee_connect/features/admin/domain/admin_models.dart';
import 'package:employee_connect/features/attendance/domain/attendance_models.dart';
import 'package:employee_connect/features/auth/presentation/auth_controller.dart';
import 'package:employee_connect/features/tasks/data/reminder_scheduler.dart';
import 'package:employee_connect/features/tasks/data/task_repository.dart';
import 'package:employee_connect/features/tasks/domain/task_models.dart';
import 'package:employee_connect/features/tasks/presentation/tasks_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'helpers/module_fakes.dart';

/// Tasks with calendar reminders and manual attendance (plan 14).
TaskItem _task(String id, DateTime due, {int? remind = 15, bool done = false, bool assigned = false}) => TaskItem(
      id: id,
      title: 'Task $id',
      dueAt: due,
      remindAt: remind == null ? null : due.subtract(Duration(minutes: remind)),
      remindMinutesBefore: remind,
      done: done,
      assigned: assigned,
    );

class _FakeTasks extends TaskRepository {
  _FakeTasks(this.items) : super(Dio());
  final List<TaskItem> items;
  final List<String> calls = [];

  @override
  Future<List<TaskItem>> list({required String from, required String to, bool assignedByMe = false}) async {
    calls.add('list $from $to $assignedByMe');
    return items;
  }

  @override
  Future<TaskItem> setDone(String id, {required bool done}) async {
    calls.add('done $id $done');
    return items.firstWhere((t) => t.id == id);
  }
}

class _RecordingScheduler implements ReminderScheduler {
  final List<List<TaskItem>> synced = [];
  @override
  Future<void> sync(List<TaskItem> upcoming) async => synced.add(upcoming);
  @override
  Future<void> clear() async {}
}

void main() {
  group('task models', () {
    test('parses the API shape', () {
      final t = TaskItem.fromJson({
        'id': 't1',
        'title': 'Submit report',
        'notes': 'Q3',
        'due_at': '2026-10-22T04:30:00Z',
        'remind_at': '2026-10-22T04:00:00Z',
        'remind_minutes_before': 30,
        'done': false,
        'assigned': true,
        'can_edit': false,
        'created_by': {'id': 'm', 'employee_id': 'MGR001', 'full_name': 'Meera'},
      });
      expect(t.dueAt, DateTime.utc(2026, 10, 22, 4, 30).toLocal());
      expect(t.remindMinutesBefore, 30);
      expect(t.assigned && !t.canEdit, isTrue);
      expect(t.createdBy!.display, 'Meera');
      expect(reminderLabel(30), '30 minutes before');
      expect(reminderLabel(null), 'No reminder');
      expect(reminderLabel(120), '2 h before');
    });

    test('groups by local day, sorted by time', () {
      final g = groupByDay([
        _task('b', DateTime(2026, 10, 22, 15)),
        _task('a', DateTime(2026, 10, 22, 9)),
        _task('c', DateTime(2026, 10, 23, 9)),
      ]);
      expect(g['2026-10-22']!.map((t) => t.id), ['a', 'b']);
      expect(g['2026-10-23']!.single.id, 'c');
    });

    test('form validation and body', () {
      expect(validateTaskForm(title: ' ', due: DateTime(2026), remindMinutesBefore: null), 'Enter a title');
      expect(validateTaskForm(title: 'x', due: null, remindMinutesBefore: null), contains('date'));
      expect(validateTaskForm(title: 'x', due: DateTime(2026), remindMinutesBefore: 20000), contains('7 days'));
      expect(validateTaskForm(title: 'x', due: DateTime(2026), remindMinutesBefore: 15), isNull);
      final b = taskBody(title: ' Call client ', notes: ' ', due: DateTime.utc(2026, 10, 22, 4, 30), remindMinutesBefore: 0);
      expect(b, {
        'title': 'Call client',
        'notes': null,
        'due_at': '2026-10-22T04:30:00.000Z',
        'remind_minutes_before': 0,
      });
      expect(taskBody(title: 'x', notes: '', due: DateTime.utc(2026), remindMinutesBefore: null, userId: 'u2')['user_id'], 'u2');
      expect(canAssignTasks(const ['task.assign_team']), isTrue);
      expect(canAssignTasks(const ['attendance.view_team']), isFalse);
    });
  });

  group('phone reminders', () {
    test('only open tasks with a future reminder, soonest first, capped', () {
      final now = DateTime(2026, 10, 21, 12);
      final picked = remindersToSchedule([
        _task('late', DateTime(2026, 10, 21, 12, 5)), // reminder 11:50 already passed
        _task('done', DateTime(2026, 10, 22, 10), done: true),
        _task('none', DateTime(2026, 10, 22, 10), remind: null),
        _task('b', DateTime(2026, 10, 23, 10)),
        _task('a', DateTime(2026, 10, 22, 10)),
      ], now,);
      expect(picked.map((t) => t.id), ['a', 'b']);
      final many = [for (var i = 0; i < 80; i++) _task('t$i', DateTime(2026, 11, 1).add(Duration(hours: i)))];
      expect(remindersToSchedule(many, now), hasLength(maxScheduledReminders));
    });

    test('notification ids are stable and positive', () {
      const id = '6b81b4a4-f8db-4f1f-8453-c954aea6b5ec';
      expect(reminderId(id), reminderId(id));
      expect(reminderId(id), isNonNegative);
      expect(reminderId(id), isNot(reminderId('other')));
    });
  });

  group('tasks screen', () {
    testWidgets('calendar shows open counts, the day list and toggles done; reminders are synced', (tester) async {
      tester.view.physicalSize = const Size(800, 2000);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);
      final now = DateTime.now();
      final today = DateTime(now.year, now.month, now.day);
      final repo = _FakeTasks([
        _task('1', today.add(const Duration(hours: 10))),
        _task('2', today.add(const Duration(hours: 15)), done: true),
      ]);
      final sched = _RecordingScheduler();
      await tester.pumpWidget(app(const TasksScreen(), [
        taskRepositoryProvider.overrideWithValue(repo),
        reminderSchedulerProvider.overrideWithValue(sched),
        authControllerProvider.overrideWith(() => FakeAuth(userWith(const []))),
      ],),);
      await tester.pumpAndSettle();

      expect(find.text('Task 1'), findsOneWidget);
      expect(find.text('Task 2'), findsOneWidget);
      expect(find.text('Assigned by me'), findsNothing); // no assign permission
      final key = '${today.year.toString().padLeft(4, '0')}-'
          '${today.month.toString().padLeft(2, '0')}-${today.day.toString().padLeft(2, '0')}';
      final cell = find.byKey(ValueKey('task-day-$key'));
      expect(find.descendant(of: cell, matching: find.text('1')), findsWidgets); // 1 open task badge
      expect(sched.synced, isNotEmpty);

      await tester.tap(find.byType(Checkbox).first);
      await tester.pumpAndSettle();
      expect(repo.calls, contains('done 1 true'));
    });

    testWidgets('managers can switch to tasks they assigned', (tester) async {
      final repo = _FakeTasks([]);
      await tester.pumpWidget(app(const TasksScreen(), [
        taskRepositoryProvider.overrideWithValue(repo),
        reminderSchedulerProvider.overrideWithValue(_RecordingScheduler()),
        authControllerProvider.overrideWith(() => FakeAuth(userWith(const ['task.assign_team']))),
      ],),);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Assigned by me'));
      await tester.pumpAndSettle();
      expect(repo.calls.where((c) => c.endsWith('true')), isNotEmpty);
    });
  });

  group('manual attendance', () {
    final now = DateTime(2026, 10, 21, 12);
    test('validation', () {
      String? v({DateTime? date, String? cin = '09:30', String? cout = '18:30', String reason = 'Forgot to punch'}) =>
          validateManualAttendance(date: date ?? DateTime(2026, 10, 20), checkIn: cin, checkOut: cout, reason: reason, now: now);
      expect(v(), isNull);
      expect(v(date: DateTime(2026, 10, 22)), contains('future'));
      expect(v(cin: null), 'Choose the check-in time');
      expect(v(cout: null), isNull);
      expect(v(reason: 'ok'), contains('reason'));
    });

    test('day parses the record with its manual flag and reason', () {
      final d = ManualAttendanceDay.fromJson({
        'work_date': '2026-10-20',
        'user': {'id': 'u', 'employee_id': 'EMP001', 'full_name': 'Ravi'},
        'record': {
          'id': 'a',
          'work_date': '2026-10-20',
          'status': 'late',
          'minutes_late': 35,
          'check_in_at': '2026-10-20T04:35:00Z',
          'manual': true,
          'manual_reason': 'Biometric down',
        },
      });
      expect(d.user!.employeeId, 'EMP001');
      expect(d.record!.manual, isTrue);
      expect(d.record!.manualReason, 'Biometric down');
      expect(d.record!.status, DayStatus.late);
      expect(ManualAttendanceDay.fromJson({'work_date': '2026-10-20', 'record': null}).record, isNull);
    });

    test('admin sections for manual attendance and announcements', () {
      expect(adminSections(const ['attendance.manual']), [AdminSection.manualAttendance]);
      expect(adminSections(const ['announcement.manage']), [AdminSection.announcements]);
    });
  });
}
