import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../attendance/domain/attendance_models.dart';
import '../data/admin_repository.dart';
import '../domain/admin_models.dart';
import 'employee_picker.dart';

/// HR / Admin: enter or correct one employee's attendance for one date
/// (`attendance.manual`). Lateness and half day are calculated by the server
/// exactly like a punch; the employee is notified and the change is audited.
class ManualAttendanceScreen extends ConsumerStatefulWidget {
  const ManualAttendanceScreen({super.key, this.now});

  /// Overridable for tests.
  final DateTime Function()? now;

  @override
  ConsumerState<ManualAttendanceScreen> createState() => _ManualAttendanceScreenState();
}

class _ManualAttendanceScreenState extends ConsumerState<ManualAttendanceScreen> {
  final _reason = TextEditingController();
  Employee? _employee;
  late DateTime _date = _today;
  String? _checkIn;
  String? _checkOut;
  ManualAttendanceDay? _day;
  bool _loading = false;
  bool _busy = false;
  String? _error;

  DateTime get _now => (widget.now ?? DateTime.now)();
  DateTime get _today => DateTime(_now.year, _now.month, _now.day);

  @override
  void dispose() {
    _reason.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    final e = _employee;
    if (e == null) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final d = await ref.read(adminRepositoryProvider).manualAttendance(e.id, ymd(_date));
      if (!mounted) return;
      final r = d.record;
      setState(() {
        _day = d;
        _checkIn = r?.checkInAt == null ? null : formatTime(r!.checkInAt!);
        _checkOut = r?.checkOutAt == null ? null : formatTime(r!.checkOutAt!);
        _reason.text = r?.manualReason ?? '';
        _loading = false;
      });
    } on ApiException catch (err) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = err.message;
      });
    }
  }

  Future<void> _pickEmployee() async {
    final e = await pickEmployee(context, title: 'Employee');
    if (e == null) return;
    setState(() => _employee = e);
    await _load();
  }

  Future<void> _pickDate() async {
    final d = await showDatePicker(
      context: context,
      initialDate: _date,
      firstDate: _today.subtract(const Duration(days: 366)),
      lastDate: _today,
    );
    if (d == null) return;
    setState(() => _date = d);
    await _load();
  }

  Future<void> _pickTime({required bool checkIn}) async {
    final cur = (checkIn ? _checkIn : _checkOut) ?? (checkIn ? '09:30' : '18:30');
    final parts = cur.split(':');
    final t = await showTimePicker(
      context: context,
      initialTime: TimeOfDay(hour: int.parse(parts[0]), minute: int.parse(parts[1])),
    );
    if (t == null) return;
    setState(() {
      final v = '${two(t.hour)}:${two(t.minute)}';
      if (checkIn) {
        _checkIn = v;
      } else {
        _checkOut = v;
      }
    });
  }

  Future<void> _save() async {
    final e = _employee;
    if (e == null || _busy) return;
    final err = validateManualAttendance(
      date: _date,
      checkIn: _checkIn,
      checkOut: _checkOut,
      reason: _reason.text,
      now: _now,
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
      final d = await ref.read(adminRepositoryProvider).saveManualAttendance(
            e.id,
            ymd(_date),
            checkIn: _checkIn!,
            checkOut: _checkOut,
            reason: _reason.text,
          );
      if (!mounted) return;
      setState(() {
        _day = d;
        _busy = false;
      });
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Attendance saved. The employee is notified.')));
    } on ApiException catch (err) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _error = err.message;
      });
    }
  }

  Future<void> _remove() async {
    final e = _employee;
    if (e == null) return;
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Remove this manual entry?'),
        content: const Text('The day will have no attendance again.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Remove')),
        ],
      ),
    );
    if (!(ok ?? false) || !mounted) return;
    setState(() => _busy = true);
    try {
      await ref.read(adminRepositoryProvider).deleteManualAttendance(e.id, ymd(_date));
      if (!mounted) return;
      setState(() => _busy = false);
      await _load();
    } on ApiException catch (err) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _error = err.message;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final rec = _day?.record;
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Manual attendance')),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Text(
              'Enter or correct attendance when an employee could not punch (forgot, phone problem, '
              'GPS issue). Late and half day are calculated automatically.',
              style: theme.textTheme.bodySmall,
            ),
            const SizedBox(height: 12),
            ListTile(
              contentPadding: EdgeInsets.zero,
              leading: const Icon(Icons.person_search_outlined),
              title: const Text('Employee'),
              subtitle: Text(_employee == null
                  ? 'Choose an employee'
                  : '${_employee!.fullName.isEmpty ? _employee!.employeeId : _employee!.fullName} (${_employee!.employeeId})',),
              trailing: const Icon(Icons.chevron_right),
              onTap: _busy ? null : _pickEmployee,
            ),
            ListTile(
              contentPadding: EdgeInsets.zero,
              leading: const Icon(Icons.calendar_today_outlined),
              title: const Text('Date'),
              subtitle: Text(formatDayLong(_date)),
              trailing: const Icon(Icons.chevron_right),
              onTap: _busy ? null : _pickDate,
            ),
            const Divider(),
            if (_employee != null) ...[
              if (_loading)
                const Padding(padding: EdgeInsets.all(24), child: Center(child: CircularProgressIndicator()))
              else
                _CurrentRecord(record: rec),
              const SizedBox(height: 12),
              Row(
                children: [
                  Expanded(
                    child: OutlinedButton.icon(
                      onPressed: _busy || _loading ? null : () => _pickTime(checkIn: true),
                      icon: const Icon(Icons.login),
                      label: Text(_checkIn == null ? 'Check-in time' : 'In $_checkIn'),
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: OutlinedButton.icon(
                      onPressed: _busy || _loading ? null : () => _pickTime(checkIn: false),
                      icon: const Icon(Icons.logout),
                      label: Text(_checkOut == null ? 'Check-out (optional)' : 'Out $_checkOut'),
                    ),
                  ),
                ],
              ),
              if (_checkOut != null)
                Align(
                  alignment: Alignment.centerRight,
                  child: TextButton(
                    onPressed: _busy ? null : () => setState(() => _checkOut = null),
                    child: const Text('Clear check-out'),
                  ),
                ),
              const SizedBox(height: 12),
              TextField(
                controller: _reason,
                enabled: !_busy,
                maxLength: 300,
                maxLines: 2,
                decoration: const InputDecoration(
                  labelText: 'Reason (required)',
                  hintText: 'e.g. Forgot to punch, confirmed by manager',
                  border: OutlineInputBorder(),
                ),
              ),
              if (_error != null)
                Padding(
                  padding: const EdgeInsets.only(bottom: 8),
                  child: Text(_error!, style: TextStyle(color: theme.colorScheme.error)),
                ),
              FilledButton(
                onPressed: _busy || _loading ? null : _save,
                style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
                child: _busy
                    ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                    : Text(rec == null ? 'Save attendance' : 'Save correction'),
              ),
              if (rec != null && rec.manual) ...[
                const SizedBox(height: 8),
                OutlinedButton.icon(
                  onPressed: _busy ? null : _remove,
                  icon: const Icon(Icons.delete_outline),
                  label: const Text('Remove manual entry'),
                ),
              ],
            ] else if (_error != null)
              Text(_error!, style: TextStyle(color: theme.colorScheme.error)),
          ],
        ),
      ),
    );
  }
}

class _CurrentRecord extends StatelessWidget {
  const _CurrentRecord({required this.record});
  final AttendanceRecord? record;

  @override
  Widget build(BuildContext context) {
    final r = record;
    final theme = Theme.of(context);
    if (r == null) {
      return Text('No attendance recorded for this day.', style: theme.textTheme.bodyMedium);
    }
    final inAt = r.checkInAt == null ? '-' : formatTime(r.checkInAt!);
    final outAt = r.checkOutAt == null ? 'not checked out' : formatTime(r.checkOutAt!);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Current: in $inAt, out $outAt', style: theme.textTheme.titleSmall),
            Text(
              '${r.status.label}${r.minutesLate > 0 ? ', ${r.minutesLate} min late' : ''}'
              '${r.manual ? ' - manual entry' : ' - punched in the app'}',
            ),
            if (r.manualReason != null && r.manualReason!.isNotEmpty) Text('Reason: ${r.manualReason}'),
          ],
        ),
      ),
    );
  }
}
