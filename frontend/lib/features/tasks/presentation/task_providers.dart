import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/utils/dates.dart';
import '../data/task_repository.dart';
import '../domain/task_models.dart';

/// Month shown on the Tasks calendar (`YYYY-MM`).
final taskMonthProvider = StateProvider.autoDispose<String>((ref) => monthKey(DateTime.now()));

/// Selected calendar day (local date).
final taskDayProvider = StateProvider.autoDispose<DateTime>((ref) {
  final n = DateTime.now();
  return DateTime(n.year, n.month, n.day);
});

/// false = my tasks, true = tasks I assigned to other people.
final taskAssignedViewProvider = StateProvider.autoDispose<bool>((ref) => false);

typedef TaskMonthQuery = ({String month, bool assignedByMe});

final tasksForMonthProvider = FutureProvider.autoDispose.family<List<TaskItem>, TaskMonthQuery>((ref, q) {
  final first = parseMonth(q.month) ?? DateTime.now();
  final last = DateTime(first.year, first.month + 1, 0);
  return ref
      .watch(taskRepositoryProvider)
      .list(from: ymd(DateTime(first.year, first.month)), to: ymd(last), assignedByMe: q.assignedByMe);
});
