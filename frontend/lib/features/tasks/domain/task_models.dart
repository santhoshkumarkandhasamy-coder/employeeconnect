import '../../../core/utils/dates.dart';
import '../../leave/domain/leave_models.dart' show PersonSummary;

/// api-contract.md section 15 (tasks with calendar reminders).
class TaskItem {
  const TaskItem({
    required this.id,
    required this.title,
    required this.dueAt,
    this.notes,
    this.remindAt,
    this.remindMinutesBefore,
    this.done = false,
    this.assigned = false,
    this.canEdit = true,
    this.user,
    this.createdBy,
  });

  final String id;
  final String title;
  final String? notes;
  final DateTime dueAt;
  final DateTime? remindAt;
  final int? remindMinutesBefore;
  final bool done;

  /// Created by someone else for the assignee (or by me for someone else).
  final bool assigned;

  /// Only the creator edits / deletes; the assignee can still mark it done.
  final bool canEdit;
  final PersonSummary? user;
  final PersonSummary? createdBy;

  bool isOverdue(DateTime now) => !done && dueAt.isBefore(now);

  factory TaskItem.fromJson(Map<String, dynamic> j) => TaskItem(
        id: '${j['id'] ?? ''}',
        title: '${j['title'] ?? ''}',
        notes: j['notes'] as String?,
        dueAt: parseTimestamp(j['due_at']) ?? DateTime.now(),
        remindAt: parseTimestamp(j['remind_at']),
        remindMinutesBefore: (j['remind_minutes_before'] as num?)?.toInt(),
        done: j['done'] as bool? ?? false,
        assigned: j['assigned'] as bool? ?? false,
        canEdit: j['can_edit'] as bool? ?? true,
        user: PersonSummary.parse(j['user']),
        createdBy: PersonSummary.parse(j['created_by']),
      );

  static List<TaskItem> parseList(Object json) {
    final raw = (json as Map<String, dynamic>)['items'];
    return [
      if (raw is List)
        for (final e in raw)
          if (e is Map<String, dynamic>) TaskItem.fromJson(e),
    ];
  }
}

/// Reminder choices offered in the form (minutes before the due time).
const List<(int?, String)> reminderOptions = [
  (null, 'No reminder'),
  (0, 'At the due time'),
  (5, '5 minutes before'),
  (15, '15 minutes before'),
  (30, '30 minutes before'),
  (60, '1 hour before'),
  (1440, '1 day before'),
];

String reminderLabel(int? minutes) {
  for (final o in reminderOptions) {
    if (o.$1 == minutes) return o.$2;
  }
  if (minutes == null) return 'No reminder';
  return minutes % 60 == 0 ? '${minutes ~/ 60} h before' : '$minutes min before';
}

/// Tasks grouped by local calendar date (`YYYY-MM-DD`), each day sorted by time.
Map<String, List<TaskItem>> groupByDay(Iterable<TaskItem> items) {
  final out = <String, List<TaskItem>>{};
  for (final t in items) {
    (out[ymd(t.dueAt)] ??= []).add(t);
  }
  for (final l in out.values) {
    l.sort((a, b) => a.dueAt.compareTo(b.dueAt));
  }
  return out;
}

const int maxTaskTitle = 150;
const int maxTaskNotes = 2000;

String? validateTaskTitle(String? v) {
  final s = v?.trim() ?? '';
  if (s.isEmpty) return 'Enter a title';
  if (s.length > maxTaskTitle) return 'Keep the title under $maxTaskTitle characters';
  return null;
}

/// First problem with the task form, or null.
String? validateTaskForm({required String title, required DateTime? due, required int? remindMinutesBefore}) {
  final t = validateTaskTitle(title);
  if (t != null) return t;
  if (due == null) return 'Choose the date and time';
  if (remindMinutesBefore != null && (remindMinutesBefore < 0 || remindMinutesBefore > 10080)) {
    return 'Choose a reminder up to 7 days before';
  }
  return null;
}

/// JSON body for POST / PUT `/tasks`. The due time is sent with its UTC offset.
Map<String, Object?> taskBody({
  required String title,
  required String notes,
  required DateTime due,
  required int? remindMinutesBefore,
  String? userId,
}) =>
    {
      'title': title.trim(),
      'notes': notes.trim().isEmpty ? null : notes.trim(),
      'due_at': due.toUtc().toIso8601String(),
      'remind_minutes_before': remindMinutesBefore,
      if (userId != null) 'user_id': userId,
    };

/// Task permissions (the server enforces them again).
bool canAssignTasks(List<String> permissions) =>
    permissions.contains('task.assign_all') || permissions.contains('task.assign_team');
