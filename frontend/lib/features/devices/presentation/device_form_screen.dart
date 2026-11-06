import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../admin/domain/admin_models.dart' show adminErrorMessage;
import '../../admin/presentation/admin_widgets.dart' show adminPermissionsProvider, NoPermissionScreen, showAdminSnack;
import '../data/device_repository.dart';
import '../domain/device_models.dart';
import 'device_job.dart';
import 'device_providers.dart';

/// Add / edit a device (`device.manage`), web console only. The communication
/// key is write-only: it is never shown, and on edit the choices are keep,
/// set a new one, or clear it. Returns true through Navigator.pop when saved.
class DeviceFormScreen extends ConsumerStatefulWidget {
  const DeviceFormScreen({super.key, this.existing, this.now, this.wait});
  final Device? existing;
  final DateTime Function()? now;
  final Future<void> Function(Duration)? wait;

  @override
  ConsumerState<DeviceFormScreen> createState() => _DeviceFormState();
}

class _DeviceFormState extends ConsumerState<DeviceFormScreen> {
  final _form = GlobalKey<FormState>();
  late final _name = TextEditingController(text: widget.existing?.name ?? '');
  late final _location = TextEditingController(text: widget.existing?.locationLabel ?? '');
  late final _ip = TextEditingController(text: widget.existing?.ip ?? '');
  late final _port = TextEditingController(text: '${widget.existing?.port ?? 4370}');
  late final _timezone = TextEditingController(text: widget.existing?.timezone ?? '');
  final _commKey = TextEditingController();
  late String _transport = widget.existing?.transport ?? 'tcp';
  late bool _enabled = widget.existing?.enabled ?? true;
  late CommKeyAction _keyAction = widget.existing == null ? CommKeyAction.clear : CommKeyAction.keep;
  late DateTime _ingestFrom = parseYmd(widget.existing?.ingestFrom) ?? _today;
  bool _busy = false;
  String? _error;
  DeviceSync? _test;
  final Map<String, String> _serverErrors = {};

  bool get _create => widget.existing == null;
  DateTime get _now => (widget.now ?? DateTime.now)();
  DateTime get _today => DateTime(_now.year, _now.month, _now.day);

  @override
  void dispose() {
    for (final c in [_name, _location, _ip, _port, _timezone, _commKey]) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> _pickDate() async {
    final d = await showDatePicker(
      context: context,
      initialDate: _ingestFrom,
      firstDate: _ingestFrom.isBefore(_today.subtract(const Duration(days: 31)))
          ? _ingestFrom
          : _today.subtract(const Duration(days: 31)),
      lastDate: _today.isBefore(_ingestFrom) ? _ingestFrom : _today,
    );
    if (d != null) setState(() => _ingestFrom = d);
  }

  Future<void> _testConnection() async {
    // Only the connection fields are needed for a test.
    final ipErr = validateDeviceIp(_ip.text) ?? validateDevicePort(_port.text);
    if (ipErr != null) {
      setState(() => _error = ipErr);
      return;
    }
    if (_keyAction == CommKeyAction.set) {
      final k = validateCommKey(_commKey.text);
      if (k != null) {
        setState(() => _error = k);
        return;
      }
    }
    final old = widget.existing;
    final addressSame = old != null &&
        old.ip == _ip.text.trim() &&
        '${old.port}' == _port.text.trim() &&
        old.transport == _transport;
    if (old != null && _keyAction == CommKeyAction.keep && old.hasCommKey && !addressSame) {
      setState(() => _error = 'The address changed, so enter the communication key again to test it.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
      _test = null;
    });
    final repo = ref.read(deviceRepositoryProvider);
    final job = await runDeviceJob(
      context,
      repo,
      () => (old != null && _keyAction == CommKeyAction.keep && addressSame)
          ? repo.testDevice(old.id)
          : repo.testSettings(
              ip: _ip.text.trim(),
              port: int.parse(_port.text.trim()),
              transport: _transport,
              commKey: _keyAction == CommKeyAction.set ? int.parse(_commKey.text.trim()) : null,
            ),
      wait: widget.wait,
    );
    if (!mounted) return;
    setState(() {
      _busy = false;
      _test = job;
    });
  }

  Future<void> _save() async {
    setState(() {
      _serverErrors.clear();
      _error = null;
    });
    // An unchanged date of an existing device was accepted before; only check what the user chose.
    final unchanged = !_create && ymd(_ingestFrom) == widget.existing!.ingestFrom;
    final dateErr = unchanged ? null : validateIngestFrom(_ingestFrom, _now);
    if (!(_form.currentState?.validate() ?? false) || dateErr != null) {
      if (dateErr != null) setState(() => _error = dateErr);
      return;
    }
    final body = deviceBody(
      name: _name.text,
      ip: _ip.text,
      port: _port.text,
      transport: _transport,
      keyAction: _keyAction,
      commKey: _commKey.text,
      enabled: _enabled,
      location: _location.text,
      timezone: _timezone.text,
      ingestFrom: _ingestFrom,
      create: _create,
    );
    setState(() => _busy = true);
    final repo = ref.read(deviceRepositoryProvider);
    try {
      if (_create) {
        await repo.createDevice(body);
      } else {
        await repo.updateDevice(widget.existing!.id, body);
      }
      // The key is write-only: drop it from memory as soon as it was sent.
      _commKey.clear();
      if (!mounted) return;
      showAdminSnack(context, 'Device saved.');
      Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        for (final d in e.details) {
          if (d.field.isNotEmpty) _serverErrors[d.field] = d.message;
        }
        _error = adminErrorMessage(e);
      });
      _form.currentState?.validate();
    }
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!canManageDevices(perms) || !ref.watch(deviceEditAllowedProvider)) {
      return const NoPermissionScreen(title: 'Device');
    }
    final theme = Theme.of(context);
    final old = widget.existing;
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: Text(_create ? 'Add device' : 'Edit device')),
        body: Form(
          key: _form,
          child: ListView(
            padding: const EdgeInsets.all(16),
            children: [
              TextFormField(
                key: const ValueKey('f-name'),
                controller: _name,
                enabled: !_busy,
                decoration: const InputDecoration(labelText: 'Name', border: OutlineInputBorder()),
                validator: (v) => validateDeviceName(v) ?? _serverErrors['name'],
              ),
              const SizedBox(height: 12),
              TextFormField(
                key: const ValueKey('f-location'),
                controller: _location,
                enabled: !_busy,
                decoration: const InputDecoration(
                  labelText: 'Location (optional)',
                  hintText: 'e.g. Ground floor',
                  border: OutlineInputBorder(),
                ),
                validator: (v) => validateDeviceLocation(v) ?? _serverErrors['location_label'],
              ),
              const SizedBox(height: 12),
              TextFormField(
                key: const ValueKey('f-ip'),
                controller: _ip,
                enabled: !_busy,
                keyboardType: TextInputType.number,
                decoration: const InputDecoration(labelText: 'IP address', border: OutlineInputBorder()),
                validator: (v) => validateDeviceIp(v) ?? _serverErrors['ip'],
              ),
              const SizedBox(height: 12),
              TextFormField(
                key: const ValueKey('f-port'),
                controller: _port,
                enabled: !_busy,
                keyboardType: TextInputType.number,
                inputFormatters: [FilteringTextInputFormatter.digitsOnly],
                decoration: const InputDecoration(labelText: 'Port', border: OutlineInputBorder()),
                validator: (v) => validateDevicePort(v) ?? _serverErrors['port'],
              ),
              const SizedBox(height: 12),
              SegmentedButton<String>(
                segments: const [
                  ButtonSegment(value: 'tcp', label: Text('TCP')),
                  ButtonSegment(value: 'udp', label: Text('UDP')),
                ],
                selected: {_transport},
                onSelectionChanged: _busy ? null : (s) => setState(() => _transport = s.first),
              ),
              const SizedBox(height: 16),
              Text('Communication key', style: theme.textTheme.titleSmall),
              Text(
                'The key is only sent to the server and is never shown again.',
                style: theme.textTheme.bodySmall,
              ),
              if (_create)
                Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: TextFormField(
                    key: const ValueKey('f-key'),
                    controller: _commKey,
                    enabled: !_busy,
                    obscureText: true,
                    enableSuggestions: false,
                    autocorrect: false,
                    keyboardType: TextInputType.number,
                    decoration: const InputDecoration(
                      labelText: 'Key set on the machine (leave empty if none)',
                      border: OutlineInputBorder(),
                    ),
                    onChanged: (v) => _keyAction = v.trim().isEmpty ? CommKeyAction.clear : CommKeyAction.set,
                    validator: (v) => (v ?? '').trim().isEmpty ? null : validateCommKey(v) ?? _serverErrors['comm_key'],
                  ),
                )
              else ...[
                RadioGroup<CommKeyAction>(
                  groupValue: _keyAction,
                  onChanged: (v) {
                    if (!_busy && v != null) setState(() => _keyAction = v);
                  },
                  child: Column(
                    children: [
                      RadioListTile<CommKeyAction>(
                        key: const ValueKey('key-keep'),
                        contentPadding: EdgeInsets.zero,
                        value: CommKeyAction.keep,
                        title: Text(old!.hasCommKey ? 'Keep the saved key' : 'Keep as is (no key)'),
                      ),
                      const RadioListTile<CommKeyAction>(
                        key: ValueKey('key-set'),
                        contentPadding: EdgeInsets.zero,
                        value: CommKeyAction.set,
                        title: Text('Set a new key'),
                      ),
                      const RadioListTile<CommKeyAction>(
                        key: ValueKey('key-clear'),
                        contentPadding: EdgeInsets.zero,
                        value: CommKeyAction.clear,
                        title: Text('Remove the key'),
                      ),
                    ],
                  ),
                ),
                if (_keyAction == CommKeyAction.set)
                  TextFormField(
                    key: const ValueKey('f-key'),
                    controller: _commKey,
                    enabled: !_busy,
                    obscureText: true,
                    enableSuggestions: false,
                    autocorrect: false,
                    keyboardType: TextInputType.number,
                    decoration: const InputDecoration(labelText: 'New key', border: OutlineInputBorder()),
                    validator: (v) => validateCommKey(v) ?? _serverErrors['comm_key'],
                  ),
              ],
              const SizedBox(height: 16),
              TextFormField(
                key: const ValueKey('f-tz'),
                controller: _timezone,
                enabled: !_busy,
                decoration: const InputDecoration(
                  labelText: 'Timezone (optional)',
                  hintText: 'Empty = office timezone, e.g. Asia/Kolkata',
                  border: OutlineInputBorder(),
                ),
                validator: (v) => validateDeviceTimezone(v) ?? _serverErrors['timezone'],
              ),
              const SizedBox(height: 12),
              ListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('Use punches from'),
                subtitle: Text('${formatDayLong(_ingestFrom)} (at most 31 days back)'),
                trailing: const Icon(Icons.calendar_today_outlined),
                onTap: _busy ? null : _pickDate,
              ),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('Enabled'),
                subtitle: const Text('A disabled device is not synced.'),
                value: _enabled,
                onChanged: _busy ? null : (v) => setState(() => _enabled = v),
              ),
              const SizedBox(height: 8),
              OutlinedButton.icon(
                key: const ValueKey('form-test'),
                onPressed: _busy ? null : _testConnection,
                icon: const Icon(Icons.network_check),
                label: const Text('Test connection'),
              ),
              if (_test != null)
                Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: Card(child: Padding(padding: const EdgeInsets.all(12), child: TestResultView(job: _test!))),
                ),
              if (_error != null)
                Padding(
                  padding: const EdgeInsets.only(top: 12),
                  child: Text(_error!, key: const ValueKey('form-error'), style: TextStyle(color: theme.colorScheme.error)),
                ),
              const SizedBox(height: 16),
              FilledButton(
                key: const ValueKey('form-save'),
                onPressed: _busy ? null : _save,
                style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
                child: _busy
                    ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                    : Text(_create ? 'Add device' : 'Save changes'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
