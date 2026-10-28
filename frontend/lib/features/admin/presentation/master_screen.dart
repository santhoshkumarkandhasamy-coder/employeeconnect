import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../attendance/data/location_service.dart';
import '../data/admin_repository.dart';
import '../domain/admin_models.dart';
import 'admin_providers.dart';
import 'admin_widgets.dart';

/// Generic list + create / edit / delete screen for departments, shifts,
/// holidays, leave types and client locations, driven by a [MasterSpec].
/// Needs `spec.permission`.
class MasterScreen extends ConsumerStatefulWidget {
  const MasterScreen({required this.spec, super.key});
  final MasterSpec spec;

  @override
  ConsumerState<MasterScreen> createState() => _MasterScreenState();
}

class _MasterScreenState extends ConsumerState<MasterScreen> {
  late int _year = DateTime.now().year;

  MasterSpec get spec => widget.spec;
  MasterKey get _key => (spec.path, spec.hasYearFilter ? _year : null);

  Future<void> _edit(MasterRecord? rec) async {
    final saved = await showDialog<bool>(
      context: context,
      builder: (_) => MasterFormDialog(
        spec: spec,
        initial: rec?.data,
        currentLocation: spec.hasGeoPicker ? () => readCurrentLocation(ref.read(locationServiceProvider)) : null,
        onSave: (body) async {
          try {
            await ref.read(adminRepositoryProvider).masterSave(spec, rec?.id, body);
            return null;
          } on ApiException catch (e) {
            return adminErrorMessage(e);
          }
        },
      ),
    );
    if (saved == true) ref.invalidate(masterListProvider(_key));
  }

  Future<void> _applyDefaults() async {
    final year = await showDialog<int>(
      context: context,
      builder: (_) => const ApplyDefaultsDialog(),
    );
    if (year == null || !mounted) return;
    try {
      final r = await ref.read(adminRepositoryProvider).applyDefaultQuotas(year);
      if (mounted) {
        showAdminSnack(
          context,
          r.created == 0
              ? 'Nothing to add: all ${r.employees} employees already have $year balances.'
              : 'Created ${r.created} balances for ${r.employees} employees.',
        );
      }
    } on ApiException catch (e) {
      if (mounted) showAdminSnack(context, adminErrorMessage(e));
    }
  }

  Future<void> _delete(MasterRecord rec) async {
    final name = '${rec.data[spec.titleKey] ?? ''}';
    final ok = await confirmAction(
      context,
      title: 'Delete $name?',
      message: spec.hasActiveFlag
          ? 'If it is in use the delete is refused; deactivate it instead.'
          : 'This cannot be undone.',
      confirmLabel: 'Delete',
    );
    if (!ok || !mounted) return;
    try {
      await ref.read(adminRepositoryProvider).masterDelete(spec, rec.id);
      ref.invalidate(masterListProvider(_key));
      if (mounted) showAdminSnack(context, 'Deleted.');
    } on ApiException catch (e) {
      if (mounted) showAdminSnack(context, adminErrorMessage(e));
    }
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!perms.contains(spec.permission)) return NoPermissionScreen(title: spec.title);
    final async = ref.watch(masterListProvider(_key));

    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(
          title: Text(spec.title),
          actions: [
            if (spec.hasApplyDefaults && perms.contains(AdminPerm.leaveBalanceManage))
              IconButton(
                tooltip: 'Apply default quotas',
                icon: const Icon(Icons.playlist_add_check),
                onPressed: _applyDefaults,
              ),
            if (spec.hasYearFilter) ...[
              IconButton(
                tooltip: 'Previous year',
                icon: const Icon(Icons.chevron_left),
                onPressed: () => setState(() => _year--),
              ),
              Center(child: Text('$_year')),
              IconButton(
                tooltip: 'Next year',
                icon: const Icon(Icons.chevron_right),
                onPressed: () => setState(() => _year++),
              ),
            ],
          ],
        ),
        floatingActionButton: FloatingActionButton.extended(
          onPressed: () => _edit(null),
          icon: const Icon(Icons.add),
          label: Text('Add ${spec.singular}'),
        ),
        body: async.when(
          skipLoadingOnReload: true,
          loading: () => const LoadingView(),
          error: (e, _) => ErrorView(
            message: adminMsg(e),
            onRetry: () => ref.invalidate(masterListProvider(_key)),
          ),
          data: (items) => RefreshIndicator(
            onRefresh: () async {
              ref.invalidate(masterListProvider(_key));
              await ref.read(masterListProvider(_key).future).then((_) {}, onError: (Object _) {});
            },
            child: items.isEmpty
                ? ListView(
                    physics: const AlwaysScrollableScrollPhysics(),
                    children: [
                      const SizedBox(height: 80),
                      EmptyView(
                        icon: Icons.inbox_outlined,
                        message: 'No ${spec.title.toLowerCase()} yet. Tap Add to create one.',
                      ),
                    ],
                  )
                : ListView.builder(
                    physics: const AlwaysScrollableScrollPhysics(),
                    padding: const EdgeInsets.fromLTRB(12, 8, 12, 96),
                    itemCount: items.length,
                    itemBuilder: (context, i) {
                      final r = items[i];
                      return Card(
                        margin: const EdgeInsets.symmetric(vertical: 4),
                        child: ListTile(
                          enabled: r.isActive,
                          title: Text('${r.data[spec.titleKey] ?? ''}'),
                          subtitle: Text(spec.subtitle(r.data)),
                          onTap: () => _edit(r),
                          trailing: PopupMenuButton<String>(
                            tooltip: 'Actions',
                            onSelected: (v) => v == 'edit' ? _edit(r) : _delete(r),
                            itemBuilder: (_) => const [
                              PopupMenuItem(value: 'edit', child: Text('Edit')),
                              PopupMenuItem(value: 'delete', child: Text('Delete')),
                            ],
                          ),
                        ),
                      );
                    },
                  ),
          ),
        ),
      ),
    );
  }
}

/// Year picker for "Apply default quotas". Pops the year (2000..2200).
class ApplyDefaultsDialog extends StatefulWidget {
  const ApplyDefaultsDialog({super.key});

  @override
  State<ApplyDefaultsDialog> createState() => _ApplyDefaultsDialogState();
}

class _ApplyDefaultsDialogState extends State<ApplyDefaultsDialog> {
  final _form = GlobalKey<FormState>();
  final _year = TextEditingController(text: '${DateTime.now().year}');

  @override
  void dispose() {
    _year.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: const Text('Apply default quotas'),
        content: Form(
          key: _form,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Text(
                'Creates the missing leave balances for every active employee from each '
                "leave type's annual quota. Existing balances are never changed.",
              ),
              const SizedBox(height: 12),
              TextFormField(
                controller: _year,
                keyboardType: TextInputType.number,
                decoration: const InputDecoration(labelText: 'Year', border: OutlineInputBorder()),
                validator: validateYear,
              ),
            ],
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            onPressed: () {
              if (_form.currentState?.validate() ?? false) {
                Navigator.pop(context, int.parse(_year.text.trim()));
              }
            },
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: const Text('Apply'),
          ),
        ],
      );
}

String? validateYear(String? v) {
  final n = int.tryParse((v ?? '').trim());
  if (n == null || n < 2000 || n > 2200) return 'Enter a year between 2000 and 2200';
  return null;
}

/// Current position for a geo master (client locations): asks for location
/// permission when needed. Throws [LocationException] with a user-facing text.
Future<LocationFix> readCurrentLocation(LocationService loc) async {
  var a = await loc.access();
  if (a == LocationAccess.denied) a = await loc.request();
  switch (a) {
    case LocationAccess.granted:
      return loc.current();
    case LocationAccess.serviceOff:
      throw const LocationException('Turn on location services and try again.');
    case LocationAccess.reducedAccuracy:
      throw const LocationException('Allow precise location for Employee Connect in Settings and try again.');
    case LocationAccess.denied:
    case LocationAccess.deniedForever:
      throw const LocationException('Location permission is needed. Allow it in Settings or type the coordinates.');
  }
}

/// Create / edit dialog for one master record. [onSave] returns an error
/// message, or null on success (the dialog then closes with `true`).
class MasterFormDialog extends StatefulWidget {
  const MasterFormDialog({
    required this.spec,
    required this.onSave,
    super.key,
    this.initial,
    this.currentLocation,
  });
  final MasterSpec spec;
  final Map<String, dynamic>? initial;
  final Future<String?> Function(Map<String, Object?> body) onSave;

  /// Fills `lat` / `lng` from the device position (only for [MasterSpec.hasGeoPicker]).
  final Future<LocationFix> Function()? currentLocation;

  @override
  State<MasterFormDialog> createState() => _MasterFormDialogState();
}

class _MasterFormDialogState extends State<MasterFormDialog> {
  final _form = GlobalKey<FormState>();
  final Map<String, TextEditingController> _text = {};
  final Map<String, Object?> _other = {};
  bool _saving = false;
  bool _locating = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    for (final f in widget.spec.fields) {
      final v = widget.initial?[f.key] ?? f.defaultValue;
      switch (f.kind) {
        case FieldKind.boolean:
          _other[f.key] = v as bool? ?? true;
        case FieldKind.weekdays:
          _other[f.key] = [if (v is List) for (final d in v) if (d is num) d.toInt()];
        default:
          final s = v == null ? '' : '$v';
          _text[f.key] = TextEditingController(
            text: f.kind == FieldKind.time && s.length >= 5 ? s.substring(0, 5) : s,
          );
      }
    }
  }

  @override
  void dispose() {
    for (final c in _text.values) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> _submit() async {
    if (!(_form.currentState?.validate() ?? false)) return;
    final body = buildMasterBody(
      widget.spec,
      {for (final e in _text.entries) e.key: e.value.text},
      _other,
    );
    final cross = validateMasterCross(widget.spec, body);
    if (cross != null) {
      setState(() => _error = cross);
      return;
    }
    setState(() {
      _saving = true;
      _error = null;
    });
    final err = await widget.onSave(body);
    if (!mounted) return;
    if (err == null) {
      Navigator.pop(context, true);
    } else {
      setState(() {
        _saving = false;
        _error = err;
      });
    }
  }

  Future<void> _useCurrentLocation() async {
    final read = widget.currentLocation;
    if (read == null || _locating) return;
    setState(() {
      _locating = true;
      _error = null;
    });
    try {
      final fix = await read();
      if (!mounted) return;
      setState(() {
        _text['lat']?.text = fix.lat.toStringAsFixed(7);
        _text['lng']?.text = fix.lng.toStringAsFixed(7);
        _locating = false;
        if (fix.isMock) _error = 'A mock location app seems to be active. Check the coordinates before saving.';
      });
    } on LocationException catch (e) {
      if (!mounted) return;
      setState(() {
        _locating = false;
        _error = e.message;
      });
    }
  }

  Future<void> _pick(FieldSpec f) async {
    final c = _text[f.key]!;
    if (f.kind == FieldKind.time) {
      final parts = c.text.split(':');
      final t = await showTimePicker(
        context: context,
        initialTime: TimeOfDay(
          hour: int.tryParse(parts.first) ?? 9,
          minute: parts.length > 1 ? int.tryParse(parts[1]) ?? 0 : 0,
        ),
        builder: (ctx, child) => MediaQuery(
          data: MediaQuery.of(ctx).copyWith(alwaysUse24HourFormat: true),
          child: child!,
        ),
      );
      if (t != null) setState(() => c.text = '${two(t.hour)}:${two(t.minute)}');
    } else {
      final now = DateTime.now();
      final d = await showDatePicker(
        context: context,
        initialDate: parseYmd(c.text) ?? now,
        firstDate: DateTime(now.year - 2),
        lastDate: DateTime(now.year + 3),
      );
      if (d != null) setState(() => c.text = ymd(d));
    }
  }

  @override
  Widget build(BuildContext context) {
    final spec = widget.spec;
    return AlertDialog(
      title: Text('${widget.initial == null ? 'Add' : 'Edit'} ${spec.singular}'),
      content: SizedBox(
        width: 400,
        child: SingleChildScrollView(
          child: Form(
            key: _form,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                for (final f in spec.fields) ...[
                  _field(f),
                  const SizedBox(height: 12),
                  if (f.key == 'lng' && widget.currentLocation != null) ...[
                    OutlinedButton.icon(
                      onPressed: _saving || _locating ? null : _useCurrentLocation,
                      icon: _locating
                          ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                          : const Icon(Icons.my_location),
                      label: const Text('Use my current location'),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      'Stand at the client site, or paste the coordinates from a map.',
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                    const SizedBox(height: 12),
                  ],
                ],
                if (_error != null)
                  Text(_error!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
              ],
            ),
          ),
        ),
      ),
      actions: [
        TextButton(
          onPressed: _saving ? null : () => Navigator.pop(context, false),
          child: const Text('Cancel'),
        ),
        FilledButton(
          onPressed: _saving ? null : _submit,
          style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
          child: const Text('Save'),
        ),
      ],
    );
  }

  Widget _field(FieldSpec f) {
    switch (f.kind) {
      case FieldKind.boolean:
        return SwitchListTile(
          contentPadding: EdgeInsets.zero,
          title: Text(f.label),
          value: _other[f.key]! as bool,
          onChanged: (v) => setState(() => _other[f.key] = v),
        );
      case FieldKind.weekdays:
        final days = (_other[f.key]! as List).cast<int>();
        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(f.label),
            Wrap(
              spacing: 6,
              children: [
                for (var d = 1; d <= 7; d++)
                  FilterChip(
                    label: Text(weekdayShort[d - 1]),
                    selected: days.contains(d),
                    onSelected: (v) => setState(() {
                      final next = [...days];
                      if (v) {
                        next.add(d);
                      } else {
                        next.remove(d);
                      }
                      _other[f.key] = next..sort();
                    }),
                  ),
              ],
            ),
          ],
        );
      case FieldKind.time:
      case FieldKind.date:
        return TextFormField(
          controller: _text[f.key],
          readOnly: true,
          onTap: () => _pick(f),
          decoration: InputDecoration(
            labelText: f.label,
            border: const OutlineInputBorder(),
            suffixIcon: Icon(f.kind == FieldKind.time ? Icons.access_time : Icons.calendar_today_outlined),
          ),
          validator: (v) => validateField(f, v),
        );
      case FieldKind.text:
      case FieldKind.integer:
      case FieldKind.decimal:
        return TextFormField(
          controller: _text[f.key],
          keyboardType: f.kind == FieldKind.text
              ? TextInputType.text
              : const TextInputType.numberWithOptions(decimal: true),
          decoration: InputDecoration(labelText: f.label, border: const OutlineInputBorder()),
          validator: (v) => validateField(f, v),
        );
    }
  }
}
