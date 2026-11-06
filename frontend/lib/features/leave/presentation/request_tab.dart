import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../data/attachment_service.dart';
import '../data/leave_repository.dart';
import '../domain/leave_logic.dart';
import '../domain/leave_models.dart';
import 'leave_providers.dart';

/// Request tab: segmented Leave | Permission. Both forms stay alive while
/// switching so half-typed input is not lost.
class RequestTab extends StatefulWidget {
  const RequestTab({super.key});

  @override
  State<RequestTab> createState() => _RequestTabState();
}

class _RequestTabState extends State<RequestTab> with AutomaticKeepAliveClientMixin {
  RequestKind _kind = RequestKind.leave;

  @override
  bool get wantKeepAlive => true;

  void _done() {
    final tabs = DefaultTabController.maybeOf(context);
    tabs?.animateTo(2);
  }

  @override
  Widget build(BuildContext context) {
    super.build(context);
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        SegmentedButton<RequestKind>(
          segments: const [
            ButtonSegment(value: RequestKind.leave, label: Text('Leave'), icon: Icon(Icons.beach_access_outlined)),
            ButtonSegment(value: RequestKind.permission, label: Text('Permission'), icon: Icon(Icons.timer_outlined)),
          ],
          selected: {_kind},
          onSelectionChanged: (s) => setState(() => _kind = s.first),
        ),
        const SizedBox(height: 16),
        Visibility(
          visible: _kind == RequestKind.leave,
          maintainState: true,
          child: LeaveForm(onSubmitted: _done),
        ),
        Visibility(
          visible: _kind == RequestKind.permission,
          maintainState: true,
          child: PermissionForm(onSubmitted: _done),
        ),
      ],
    );
  }
}

/// Tappable read-only field used for dates and times.
class PickerField extends StatelessWidget {
  const PickerField({
    required this.label,
    required this.value,
    required this.onTap,
    required this.icon,
    super.key,
  });
  final String label;
  final String? value;
  final VoidCallback? onTap;
  final IconData icon;

  @override
  Widget build(BuildContext context) => Semantics(
        button: true,
        label: '$label: ${value ?? 'not set'}',
        excludeSemantics: true,
        onTap: onTap,
        child: InkWell(
          onTap: onTap,
          child: InputDecorator(
            decoration: InputDecoration(
              labelText: label,
              border: const OutlineInputBorder(),
              suffixIcon: Icon(icon),
            ),
            child: Text(value ?? 'Select', overflow: TextOverflow.ellipsis),
          ),
        ),
      );
}

class _ErrorBox extends StatelessWidget {
  const _ErrorBox(this.message);
  final String message;

  @override
  Widget build(BuildContext context) {
    final c = Theme.of(context).colorScheme;
    return Semantics(
      liveRegion: true,
      child: Container(
        padding: const EdgeInsets.all(12),
        margin: const EdgeInsets.only(bottom: 12),
        decoration: BoxDecoration(color: c.errorContainer, borderRadius: BorderRadius.circular(8)),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(Icons.error_outline, color: c.onErrorContainer),
            const SizedBox(width: 8),
            Expanded(child: Text(message, style: TextStyle(color: c.onErrorContainer))),
          ],
        ),
      ),
    );
  }
}

// ---- leave form ----------------------------------------------------------------

class LeaveForm extends ConsumerStatefulWidget {
  const LeaveForm({required this.onSubmitted, super.key});
  final VoidCallback onSubmitted;

  @override
  ConsumerState<LeaveForm> createState() => _LeaveFormState();
}

class _LeaveFormState extends ConsumerState<LeaveForm> {
  final _formKey = GlobalKey<FormState>();
  final _reason = TextEditingController();
  LeaveType? _type;
  DateTime? _from;
  DateTime? _to;
  bool _half = false;
  String _session = 'first';
  PickedAttachment? _file;

  /// Server id of the uploaded document. Kept after a partial failure so every
  /// part of a year split (and retries) carries the same attachment.
  String? _fileId;
  DateTime? _retryUntil;
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    final f = _file;
    if (f != null) ref.read(attachmentServiceProvider).discard(f.path);
    _reason.dispose();
    super.dispose();
  }

  DateTime get _today => dateOnly(DateTime.now());

  Future<void> _pickDate({required bool from}) async {
    final init = (from ? _from : _to) ?? _from ?? _today;
    final d = await showDatePicker(
      context: context,
      initialDate: init,
      firstDate: _today.subtract(const Duration(days: defaultBackdateDays)),
      lastDate: leaveLastDate(_today),
      helpText: from ? 'From date' : 'To date',
    );
    if (d == null) return;
    setState(() {
      if (from) {
        _from = d;
        if (_to == null || _to!.isBefore(d) || _half) _to = d;
      } else {
        _to = d;
      }
      _error = null;
    });
  }

  Future<void> _pickFile() async {
    final src = await showModalBottomSheet<AttachmentSource>(
      context: context,
      showDragHandle: true,
      builder: (ctx) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              leading: const Icon(Icons.photo_camera_outlined),
              title: const Text('Take a photo'),
              onTap: () => Navigator.pop(ctx, AttachmentSource.camera),
            ),
            ListTile(
              leading: const Icon(Icons.photo_library_outlined),
              title: const Text('Choose an image'),
              subtitle: const Text('JPEG or PNG, up to 10 MB'),
              onTap: () => Navigator.pop(ctx, AttachmentSource.gallery),
            ),
          ],
        ),
      ),
    );
    if (src == null) return;
    final svc = ref.read(attachmentServiceProvider);
    final picked = await svc.pick(src);
    if (picked == null || !mounted) return;
    final old = _file;
    if (old != null) await svc.discard(old.path);
    setState(() {
      _file = picked;
      _fileId = null; // a new document must be uploaded
    });
  }

  Future<void> _submit({required bool split}) async {
    if (_busy) return;
    final wait = _retryUntil?.difference(DateTime.now());
    if (wait != null && !wait.isNegative && wait.inSeconds > 0) {
      setState(() => _error = 'Too many attempts. Try again in ${waitText(wait)}.');
      return;
    }
    if (!(_formKey.currentState?.validate() ?? false)) return;
    final err = validateLeaveForm(
      type: _type,
      from: _from,
      to: _to,
      halfDay: _half,
      halfDaySession: _session,
      reason: _reason.text,
    );
    if (err != null) {
      setState(() => _error = err);
      return;
    }
    final ranges = splitByYear(_from!, _to!);
    if (ranges.length > 1 && !split) {
      setState(() => _error = yearCrossingMessage(_from!, _to!));
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    final repo = ref.read(leaveRepositoryProvider);
    final svc = ref.read(attachmentServiceProvider);
    final file = _file;
    if (file != null && _fileId == null) {
      try {
        _fileId = await repo.uploadAttachment(file.path, filename: file.name);
      } on ApiException catch (e) {
        if (!mounted) return;
        setState(() {
          _busy = false;
          _error = 'Could not upload the attachment: ${applyErrorMessage(e)}';
        });
        return;
      }
    }
    final fileId = _fileId; // same id for EVERY part of a split, never nulled on failure
    var done = 0;
    String? failure;
    for (final r in ranges) {
      try {
        await repo.applyLeave(
          leaveTypeId: _type!.id,
          fromDate: ymd(r.from),
          toDate: ymd(r.to),
          halfDay: _half,
          halfDaySession: _session,
          reason: _reason.text.trim(),
          attachmentFileId: fileId,
        );
        done++;
      } on ApiException catch (e) {
        failure = applyErrorMessage(e);
        final ra = e.retryAfter;
        if (e.code == 'RATE_LIMITED' && ra != null) _retryUntil = DateTime.now().add(ra);
        break;
      }
    }
    if (done > 0) refreshLeaveData(ref);
    if (!mounted) return;
    if (failure != null) {
      setState(() {
        _busy = false;
        _error = done == 0
            ? failure
            : 'Request $done of ${ranges.length} was submitted, the rest failed: $failure';
        if (done > 0) {
          // Keep the unsent part so it can be retried; the document (_file and
          // _fileId) stays attached to it.
          _from = ranges[done].from;
          _to = ranges.last.to;
        }
      });
      return;
    }
    if (file != null) await svc.discard(file.path);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(ranges.length > 1 ? 'Leave submitted as ${ranges.length} requests.' : 'Leave request submitted.'),
    ),);
    _formKey.currentState?.reset();
    _reason.clear();
    setState(() {
      _busy = false;
      _from = _to = null;
      _half = false;
      _file = null;
      _fileId = null;
      _error = null;
    });
    widget.onSubmitted();
  }

  @override
  Widget build(BuildContext context) {
    final types = ref.watch(leaveTypesProvider);
    return types.when(
      loading: () => const SizedBox(height: 200, child: LoadingView(label: 'Loading leave types')),
      error: (e, _) => SizedBox(
        height: 240,
        child: ErrorView(
          message: e is ApiException ? e.message : 'Could not load leave types.',
          onRetry: () => ref.invalidate(leaveTypesProvider),
        ),
      ),
      data: (c) {
        final list = c.value;
        if (list.isEmpty) {
          return const SizedBox(height: 200, child: EmptyView(message: 'No leave types are available.'));
        }
        return _buildForm(context, list);
      },
    );
  }

  Widget _buildForm(BuildContext context, List<LeaveType> list) {
    final theme = Theme.of(context);
    final type = _type == null ? null : list.where((t) => t.id == _type!.id).firstOrNull;
    final from = _from;
    final to = _to;
    final crossing = from != null && to != null && !to.isBefore(from) && crossesYear(from, to);
    final span = from != null && to != null && !to.isBefore(from) ? calendarSpan(from, to) : null;
    final bal = type == null ? null : ref.watch(leaveBalanceProvider).valueOrNull?.value.forType(type.id);
    final hasFile = _file != null || _fileId != null;
    final needsAttachment = type?.requiresAttachmentAfterDays != null &&
        span != null &&
        span > type!.requiresAttachmentAfterDays!;
    final estimate = _half ? 0.5 : span?.toDouble();

    return Form(
      key: _formKey,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (_error != null) _ErrorBox(_error!),
          DropdownButtonFormField<String>(
            initialValue: type?.id,
            isExpanded: true,
            decoration: const InputDecoration(labelText: 'Leave type', border: OutlineInputBorder()),
            items: [for (final t in list) DropdownMenuItem(value: t.id, child: Text(t.name))],
            validator: (v) => v == null ? 'Choose a leave type' : null,
            onChanged: _busy
                ? null
                : (id) => setState(() {
                      _type = list.firstWhere((t) => t.id == id);
                      if (!_type!.allowHalfDay) _half = false;
                      _error = null;
                    }),
          ),
          if (bal != null)
            Padding(
              padding: const EdgeInsets.only(top: 4, left: 4),
              child: Text(
                'Balance: ${daysLabel(bal.remaining)} remaining'
                '${bal.pending > 0 ? ' (${formatDays(bal.pending)} pending)' : ''}',
                style: theme.textTheme.bodySmall,
              ),
            ),
          const SizedBox(height: 12),
          Row(
            children: [
              Expanded(
                child: PickerField(
                  label: 'From',
                  value: from == null ? null : formatDayLong(from),
                  icon: Icons.calendar_today_outlined,
                  onTap: _busy ? null : () => _pickDate(from: true),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: PickerField(
                  label: 'To',
                  value: to == null ? null : formatDayLong(to),
                  icon: Icons.calendar_today_outlined,
                  onTap: _busy || _half ? null : () => _pickDate(from: false),
                ),
              ),
            ],
          ),
          if (type?.allowHalfDay ?? false) ...[
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              title: const Text('Half day'),
              value: _half,
              onChanged: _busy
                  ? null
                  : (v) => setState(() {
                        _half = v;
                        if (v && _from != null) _to = _from;
                      }),
            ),
            if (_half)
              SegmentedButton<String>(
                segments: const [
                  ButtonSegment(value: 'first', label: Text('First half')),
                  ButtonSegment(value: 'second', label: Text('Second half')),
                ],
                selected: {_session},
                onSelectionChanged: (s) => setState(() => _session = s.first),
              ),
          ],
          if (estimate != null)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(
                _half
                    ? 'Half day: 0.5 day will be deducted.'
                    : '$span calendar ${span == 1 ? 'day' : 'days'} selected. '
                        'Weekly offs and holidays are not deducted.',
                style: theme.textTheme.bodySmall,
              ),
            ),
          if (bal != null && estimate != null && bal.quota < 365 && estimate > bal.remaining)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text(
                'This may be more than your remaining balance.',
                style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.error),
              ),
            ),
          if (type != null)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text(mergedRequestsHint, style: theme.textTheme.bodySmall),
            ),
          if (crossing) ...[
            const SizedBox(height: 12),
            _ErrorBox(yearCrossingMessage(from, to)),
            OutlinedButton(
              onPressed: _busy ? null : () => _submit(split: true),
              child: Text('Submit as ${splitByYear(from, to).length} requests'),
            ),
          ],
          const SizedBox(height: 12),
          TextFormField(
            controller: _reason,
            enabled: !_busy,
            maxLines: 3,
            maxLength: maxReasonLength,
            textInputAction: TextInputAction.newline,
            decoration: const InputDecoration(labelText: 'Reason', border: OutlineInputBorder()),
            validator: validateReason,
          ),
          if (needsAttachment && !hasFile)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Text(
                '${type.name} over ${type.requiresAttachmentAfterDays} days needs a supporting document.',
                style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.error),
              ),
            ),
          if (_file == null)
            OutlinedButton.icon(
              onPressed: _busy ? null : _pickFile,
              icon: const Icon(Icons.attach_file),
              label: const Text('Attach a document (photo)'),
            )
          else
            ListTile(
              contentPadding: EdgeInsets.zero,
              leading: const Icon(Icons.insert_drive_file_outlined),
              title: Text(_file!.name, overflow: TextOverflow.ellipsis),
              trailing: IconButton(
                tooltip: 'Remove attachment',
                icon: const Icon(Icons.close),
                onPressed: _busy
                    ? null
                    : () {
                        ref.read(attachmentServiceProvider).discard(_file!.path);
                        setState(() {
                          _file = null;
                          _fileId = null;
                        });
                      },
              ),
            ),
          const SizedBox(height: 16),
          FilledButton(
            onPressed: _busy || crossing ? null : () => _submit(split: false),
            style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
            child: _busy
                ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('Submit leave request'),
          ),
        ],
      ),
    );
  }
}

// ---- permission form --------------------------------------------------------------

class PermissionForm extends ConsumerStatefulWidget {
  const PermissionForm({required this.onSubmitted, super.key});
  final VoidCallback onSubmitted;

  @override
  ConsumerState<PermissionForm> createState() => _PermissionFormState();
}

class _PermissionFormState extends ConsumerState<PermissionForm> {
  final _formKey = GlobalKey<FormState>();
  final _reason = TextEditingController();
  DateTime? _date;
  String? _fromTime;
  String? _toTime;
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _reason.dispose();
    super.dispose();
  }

  Future<void> _pickDate() async {
    final today = dateOnly(DateTime.now());
    final d = await showDatePicker(
      context: context,
      initialDate: _date ?? today,
      firstDate: today,
      lastDate: DateTime(today.year + 1, 12, 31),
      helpText: 'Permission date',
    );
    if (d != null) setState(() => _date = d);
  }

  Future<void> _pickTime({required bool from}) async {
    final cur = parseHm(from ? _fromTime : _toTime) ?? 9 * 60;
    final t = await showTimePicker(
      context: context,
      initialTime: TimeOfDay(hour: cur ~/ 60, minute: cur % 60),
      helpText: from ? 'From time' : 'To time',
    );
    if (t == null) return;
    setState(() {
      final v = formatHm(t.hour, t.minute);
      if (from) {
        _fromTime = v;
      } else {
        _toTime = v;
      }
    });
  }

  Future<void> _submit() async {
    if (_busy) return;
    if (!(_formKey.currentState?.validate() ?? false)) return;
    final err = validatePermissionForm(
      date: _date,
      fromTime: _fromTime,
      toTime: _toTime,
      reason: _reason.text,
      today: DateTime.now(),
    );
    if (err != null) {
      setState(() => _error = err);
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref.read(leaveRepositoryProvider).applyPermission(
            date: ymd(_date!),
            fromTime: _fromTime!,
            toTime: _toTime!,
            reason: _reason.text.trim(),
          );
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _error = leaveErrorMessage(e);
      });
      return;
    }
    refreshLeaveData(ref);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Permission request submitted.')));
    _formKey.currentState?.reset();
    _reason.clear();
    setState(() {
      _busy = false;
      _date = _fromTime = _toTime = null;
    });
    widget.onSubmitted();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final mins = minutesBetween(_fromTime, _toTime);
    final bal = ref.watch(leaveBalanceProvider).valueOrNull?.value.permission;
    return Form(
      key: _formKey,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (_error != null) _ErrorBox(_error!),
          if (bal != null)
            Padding(
              padding: const EdgeInsets.only(bottom: 12),
              child: Text(
                'This month: ${bal.remainingCount} requests and ${formatDuration(bal.remainingMinutes)} left '
                '(pending included).',
                style: theme.textTheme.bodySmall,
              ),
            ),
          PickerField(
            label: 'Date',
            value: _date == null ? null : formatDayLong(_date!),
            icon: Icons.calendar_today_outlined,
            onTap: _busy ? null : _pickDate,
          ),
          Padding(
            padding: const EdgeInsets.only(top: 4, left: 4),
            child: Text(overnightShiftHint, style: theme.textTheme.bodySmall),
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              Expanded(
                child: PickerField(
                  label: 'From time',
                  value: _fromTime,
                  icon: Icons.schedule,
                  onTap: _busy ? null : () => _pickTime(from: true),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: PickerField(
                  label: 'To time',
                  value: _toTime,
                  icon: Icons.schedule,
                  onTap: _busy ? null : () => _pickTime(from: false),
                ),
              ),
            ],
          ),
          if (mins != null)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(
                mins > 0 ? 'Duration: ${formatDuration(mins)}' : 'The to time must be after the from time.',
                style: theme.textTheme.bodySmall?.copyWith(color: mins > 0 ? null : theme.colorScheme.error),
              ),
            ),
          const SizedBox(height: 12),
          TextFormField(
            controller: _reason,
            enabled: !_busy,
            maxLines: 3,
            maxLength: maxReasonLength,
            decoration: const InputDecoration(labelText: 'Reason', border: OutlineInputBorder()),
            validator: validateReason,
          ),
          const SizedBox(height: 8),
          FilledButton(
            onPressed: _busy ? null : _submit,
            style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
            child: _busy
                ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('Submit permission request'),
          ),
        ],
      ),
    );
  }
}
