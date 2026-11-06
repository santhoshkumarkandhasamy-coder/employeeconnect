import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/month_selector.dart';
import '../../../core/widgets/state_views.dart';
import '../../auth/presentation/auth_controller.dart';
import '../../auth/presentation/auth_state.dart';
import '../data/reminder_scheduler.dart';
import '../data/task_repository.dart';
import '../domain/task_models.dart';
import 'task_form_screen.dart';
import 'task_providers.dart';

/// Tasks & reminders: a month calendar with a dot per day that has open tasks,
/// and the selected day's tasks below. Managers / HR / Admin also see the
/// tasks they assigned to others.
class TasksScreen extends ConsumerWidget {
  const TasksScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final perms = ref.watch(
      authControllerProvider.select((s) => s is AuthAuthenticated ? s.user.permissions : const <String>[]),
    );
    final canAssign = canAssignTasks(perms);
    final month = ref.watch(taskMonthProvider);
    final assigned = canAssign && ref.watch(taskAssignedViewProvider);
    final query = (month: month, assignedByMe: assigned);
    final tasks = ref.watch(tasksForMonthProvider(query));
    final day = ref.watch(taskDayProvider);
    ref.watch(taskReminderSyncProvider); // keep the phone's reminders in step with the server

    Future<void> refresh() async {
      ref.invalidate(tasksForMonthProvider(query));
      await ref.read(tasksForMonthProvider(query).future).then((_) {}, onError: (Object _) {});
    }

    Future<void> openForm({TaskItem? task}) async {
      final saved = await Navigator.of(context).push<bool>(
        MaterialPageRoute(builder: (_) => TaskFormScreen(initial: task, initialDate: day, canAssign: canAssign)),
      );
      if (saved ?? false) {
        ref
          ..invalidate(tasksForMonthProvider)
          ..invalidate(taskReminderSyncProvider);
      }
    }

    return Scaffold(
      appBar: AppBar(title: const Text('Tasks & reminders')),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () => openForm(),
        icon: const Icon(Icons.add_task),
        label: const Text('Add task'),
      ),
      body: Column(
        children: [
          if (canAssign)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 0),
              child: SegmentedButton<bool>(
                segments: const [
                  ButtonSegment(value: false, label: Text('My tasks')),
                  ButtonSegment(value: true, label: Text('Assigned by me')),
                ],
                selected: {assigned},
                onSelectionChanged: (s) => ref.read(taskAssignedViewProvider.notifier).state = s.first,
              ),
            ),
          MonthSelector(
            month: month,
            allowFuture: true,
            onChanged: (m) {
              ref.read(taskMonthProvider.notifier).state = m;
              final d = parseMonth(m);
              if (d != null) ref.read(taskDayProvider.notifier).state = d;
            },
          ),
          Expanded(
            child: tasks.when(
              loading: () => const LoadingView(label: 'Loading tasks'),
              error: (e, _) => ErrorView(
                message: e is ApiException ? e.message : 'Could not load tasks.',
                onRetry: refresh,
              ),
              data: (list) {
                final byDay = groupByDay(list);
                final dayTasks = byDay[ymd(day)] ?? const <TaskItem>[];
                return RefreshIndicator(
                  onRefresh: refresh,
                  child: ListView(
                    padding: const EdgeInsets.fromLTRB(16, 0, 16, 96),
                    children: [
                      TaskMonthGrid(
                        month: month,
                        selected: day,
                        openCounts: {
                          for (final e in byDay.entries) e.key: e.value.where((t) => !t.done).length,
                        },
                        onTap: (d) => ref.read(taskDayProvider.notifier).state = d,
                      ),
                      const SizedBox(height: 12),
                      Text(formatDayLong(day), style: Theme.of(context).textTheme.titleMedium),
                      const SizedBox(height: 4),
                      if (dayTasks.isEmpty)
                        const Padding(
                          padding: EdgeInsets.symmetric(vertical: 24),
                          child: Center(child: Text('No tasks on this day. Tap "Add task" to plan one.')),
                        )
                      else
                        for (final t in dayTasks)
                          TaskTile(
                            task: t,
                            showAssignee: assigned,
                            onTap: () => t.canEdit ? openForm(task: t) : _showDetails(context, t),
                            onToggle: (v) async {
                              final messenger = ScaffoldMessenger.of(context);
                              try {
                                await ref.read(taskRepositoryProvider).setDone(t.id, done: v);
                              } on ApiException catch (e) {
                                messenger.showSnackBar(SnackBar(content: Text(e.message)));
                              }
                              ref
                                ..invalidate(tasksForMonthProvider(query))
                                ..invalidate(taskReminderSyncProvider);
                            },
                          ),
                    ],
                  ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }

  void _showDetails(BuildContext context, TaskItem t) => showModalBottomSheet<void>(
        context: context,
        showDragHandle: true,
        builder: (ctx) => SafeArea(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(t.title, style: Theme.of(ctx).textTheme.titleLarge),
                const SizedBox(height: 8),
                Text('Due ${formatDayLong(t.dueAt)} at ${formatTime(t.dueAt)}'),
                Text('Reminder: ${reminderLabel(t.remindMinutesBefore)}'),
                if (t.createdBy != null) Text('Assigned by ${t.createdBy!.display}'),
                if (t.notes != null && t.notes!.isNotEmpty) ...[
                  const SizedBox(height: 8),
                  SelectableText(t.notes!),
                ],
              ],
            ),
          ),
        ),
      );
}

/// Month grid (Mon..Sun) with a badge of open tasks per day.
class TaskMonthGrid extends StatelessWidget {
  const TaskMonthGrid({
    required this.month,
    required this.selected,
    required this.openCounts,
    required this.onTap,
    super.key,
    this.today,
  });
  final String month;
  final DateTime selected;

  /// `YYYY-MM-DD` -> open task count.
  final Map<String, int> openCounts;
  final ValueChanged<DateTime> onTap;
  final DateTime? today;

  @override
  Widget build(BuildContext context) {
    final first = parseMonth(month) ?? DateTime.now();
    final days = daysInMonth(first.year, first.month);
    final lead = DateTime(first.year, first.month).weekday - 1; // Monday first
    final now = today ?? DateTime.now();
    final scheme = Theme.of(context).colorScheme;
    const names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    return Column(
      children: [
        Row(
          children: [
            for (final n in names)
              Expanded(child: Center(child: Text(n, style: Theme.of(context).textTheme.labelSmall))),
          ],
        ),
        const SizedBox(height: 4),
        GridView.count(
          crossAxisCount: 7,
          shrinkWrap: true,
          physics: const NeverScrollableScrollPhysics(),
          childAspectRatio: 1,
          children: [
            for (var i = 0; i < lead; i++) const SizedBox.shrink(),
            for (var d = 1; d <= days; d++)
              Builder(builder: (context) {
                final date = DateTime(first.year, first.month, d);
                final key = ymd(date);
                final count = openCounts[key] ?? 0;
                final isSel = ymd(selected) == key;
                final isToday = ymd(now) == key;
                return Semantics(
                  button: true,
                  selected: isSel,
                  label: '${formatDayLong(date)}${count > 0 ? ', $count open ${count == 1 ? 'task' : 'tasks'}' : ''}',
                  excludeSemantics: true,
                  child: InkWell(
                    key: ValueKey('task-day-$key'),
                    borderRadius: BorderRadius.circular(8),
                    onTap: () => onTap(date),
                    child: Container(
                      margin: const EdgeInsets.all(2),
                      decoration: BoxDecoration(
                        color: isSel ? scheme.primaryContainer : null,
                        border: isToday ? Border.all(color: scheme.primary) : null,
                        borderRadius: BorderRadius.circular(8),
                      ),
                      child: Column(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: [
                          Text('$d'),
                          if (count > 0)
                            Badge(label: Text('$count'), backgroundColor: scheme.secondary)
                          else
                            const SizedBox(height: 16),
                        ],
                      ),
                    ),
                  ),
                );
              },),
          ],
        ),
      ],
    );
  }
}

class TaskTile extends StatelessWidget {
  const TaskTile({required this.task, required this.onTap, required this.onToggle, super.key, this.showAssignee = false});
  final TaskItem task;
  final VoidCallback onTap;
  final ValueChanged<bool> onToggle;
  final bool showAssignee;

  @override
  Widget build(BuildContext context) {
    final t = task;
    final theme = Theme.of(context);
    final overdue = t.isOverdue(DateTime.now());
    final who = showAssignee
        ? (t.user == null ? null : 'For ${t.user!.display}')
        : (t.assigned && t.createdBy != null ? 'From ${t.createdBy!.display}' : null);
    return Card(
      child: ListTile(
        key: ValueKey('task-${t.id}'),
        leading: Checkbox(
          value: t.done,
          onChanged: (v) => onToggle(v ?? false),
          semanticLabel: t.done ? 'Mark not done' : 'Mark done',
        ),
        title: Text(
          t.title,
          style: t.done ? const TextStyle(decoration: TextDecoration.lineThrough) : null,
        ),
        subtitle: Text(
          [
            formatTime(t.dueAt),
            if (t.remindMinutesBefore != null) reminderLabel(t.remindMinutesBefore),
            if (who != null) who,
            if (overdue) 'Overdue',
          ].join(' - '),
          style: overdue ? TextStyle(color: theme.colorScheme.error) : null,
        ),
        trailing: t.remindMinutesBefore != null ? const Icon(Icons.notifications_active_outlined) : null,
        onTap: onTap,
      ),
    );
  }
}
