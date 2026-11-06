import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../admin/presentation/employee_picker.dart';
import '../data/task_repository.dart';
import '../domain/task_models.dart';

/// Create or edit a task. [canAssign] offers "Assign to" on create.
class TaskFormScreen extends ConsumerStatefulWidget {
  const TaskFormScreen({required this.initialDate, required this.canAssign, super.key, this.initial});
  final TaskItem? initial;
  final DateTime initialDate;
  final bool canAssign;

  @override
  ConsumerState<TaskFormScreen> createState() => _TaskFormScreenState();
}

class _TaskFormScreenState extends ConsumerState<TaskFormScreen> {
  final _form = GlobalKey<FormState>();
  late final _title = TextEditingController(text: widget.initial?.title ?? '');
  late final _notes = TextEditingController(text: widget.initial?.notes ?? '');
  late DateTime _date = widget.initial?.dueAt ?? widget.initialDate;
  late TimeOfDay _time = widget.initial == null
      ? const TimeOfDay(hour: 10, minute: 0)
      : TimeOfDay.fromDateTime(widget.initial!.dueAt);
  late int? _remind = widget.initial == null ? 15 : widget.initial!.remindMinutesBefore;
  String? _assigneeId;
  String _assigneeName = 'Me';
  bool _busy = false;
  String? _error;

  bool get _editing => widget.initial != null;

  @override
  void dispose() {
    _title.dispose();
    _notes.dispose();
    super.dispose();
  }

  DateTime get _due => DateTime(_date.year, _date.month, _date.day, _time.hour, _time.minute);

  Future<void> _save() async {
    if (_busy || !(_form.currentState?.validate() ?? false)) return;
    final err = validateTaskForm(title: _title.text, due: _due, remindMinutesBefore: _remind);
    if (err != null) {
      setState(() => _error = err);
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    final repo = ref.read(taskRepositoryProvider);
    final body = taskBody(
      title: _title.text,
      notes: _notes.text,
      due: _due,
      remindMinutesBefore: _remind,
      userId: _editing ? null : _assigneeId,
    );
    try {
      if (_editing) {
        await repo.update(widget.initial!.id, body);
      } else {
        await repo.create(body);
      }
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _error = e.message;
      });
      return;
    }
    if (mounted) Navigator.pop(context, true);
  }

  Future<void> _delete() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Delete this task?'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Delete')),
        ],
      ),
    );
    if (!(ok ?? false) || !mounted) return;
    setState(() => _busy = true);
    try {
      await ref.read(taskRepositoryProvider).delete(widget.initial!.id);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _error = e.message;
      });
      return;
    }
    if (mounted) Navigator.pop(context, true);
  }

  Future<void> _pickDate() async {
    final now = DateTime.now();
    final d = await showDatePicker(
      context: context,
      initialDate: _date,
      firstDate: DateTime(now.year - 1),
      lastDate: DateTime(now.year + 3),
    );
    if (d != null) setState(() => _date = d);
  }

  Future<void> _pickTime() async {
    final t = await showTimePicker(context: context, initialTime: _time);
    if (t != null) setState(() => _time = t);
  }

  Future<void> _pickAssignee() async {
    final e = await pickEmployee(context, title: 'Assign task to');
    if (e == null) return;
    setState(() {
      _assigneeId = e.id;
      _assigneeName = e.fullName.isEmpty ? e.employeeId : '${e.fullName} (${e.employeeId})';
    });
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(
        title: Text(_editing ? 'Edit task' : 'New task'),
        actions: [
          if (_editing)
            IconButton(tooltip: 'Delete task', icon: const Icon(Icons.delete_outline), onPressed: _busy ? null : _delete),
        ],
      ),
      body: Form(
        key: _form,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            if (_error != null)
              Padding(
                padding: const EdgeInsets.only(bottom: 12),
                child: Text(_error!, style: TextStyle(color: theme.colorScheme.error)),
              ),
            TextFormField(
              controller: _title,
              enabled: !_busy,
              maxLength: maxTaskTitle,
              decoration: const InputDecoration(labelText: 'Title', border: OutlineInputBorder()),
              validator: validateTaskTitle,
            ),
            TextFormField(
              controller: _notes,
              enabled: !_busy,
              maxLines: 4,
              maxLength: maxTaskNotes,
              decoration: const InputDecoration(labelText: 'Notes (optional)', border: OutlineInputBorder()),
            ),
            const SizedBox(height: 8),
            Row(
              children: [
                Expanded(
                  child: OutlinedButton.icon(
                    onPressed: _busy ? null : _pickDate,
                    icon: const Icon(Icons.calendar_today_outlined),
                    label: Text(formatDayLong(_date)),
                  ),
                ),
                const SizedBox(width: 12),
                OutlinedButton.icon(
                  onPressed: _busy ? null : _pickTime,
                  icon: const Icon(Icons.schedule),
                  label: Text('${two(_time.hour)}:${two(_time.minute)}'),
                ),
              ],
            ),
            const SizedBox(height: 16),
            DropdownButtonFormField<int?>(
              initialValue: reminderOptions.any((o) => o.$1 == _remind) ? _remind : null,
              decoration: const InputDecoration(labelText: 'Reminder', border: OutlineInputBorder()),
              items: [for (final o in reminderOptions) DropdownMenuItem(value: o.$1, child: Text(o.$2))],
              onChanged: _busy ? null : (v) => setState(() => _remind = v),
            ),
            const SizedBox(height: 4),
            Text(
              'You get a notification on your phone at the reminder time.',
              style: theme.textTheme.bodySmall,
            ),
            if (widget.canAssign && !_editing) ...[
              const SizedBox(height: 12),
              ListTile(
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.person_add_alt_outlined),
                title: const Text('Assign to'),
                subtitle: Text(_assigneeName),
                trailing: _assigneeId == null
                    ? null
                    : IconButton(
                        tooltip: 'Assign to me',
                        icon: const Icon(Icons.close),
                        onPressed: () => setState(() {
                          _assigneeId = null;
                          _assigneeName = 'Me';
                        }),
                      ),
                onTap: _busy ? null : _pickAssignee,
              ),
            ],
            if (_editing && widget.initial!.assigned && widget.initial!.user != null)
              Padding(
                padding: const EdgeInsets.only(top: 12),
                child: Text('Assigned to ${widget.initial!.user!.display}', style: theme.textTheme.bodyMedium),
              ),
            const SizedBox(height: 24),
            FilledButton(
              onPressed: _busy ? null : _save,
              style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
              child: _busy
                  ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                  : Text(_editing ? 'Save changes' : 'Save task'),
            ),
          ],
        ),
      ),
    );
  }
}
