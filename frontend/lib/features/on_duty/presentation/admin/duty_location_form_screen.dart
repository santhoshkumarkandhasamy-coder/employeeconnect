import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../core/api/api_exception.dart';
import '../../../../core/security/screen_protect.dart';
import '../../../admin/domain/admin_models.dart' show adminErrorMessage;
import '../../../admin/presentation/admin_widgets.dart' show showAdminSnack;
import '../../data/on_duty_repository.dart';
import '../../domain/duty_admin_models.dart';
import '../../domain/on_duty_models.dart';
import 'duty_admin_providers.dart';

/// Create or edit a duty location. Coordinates are typed or pasted (a maps
/// link works). "Use my current location" appears only on the phone app, where
/// [readPosition] is supplied; the web console has none.
class DutyLocationFormScreen extends ConsumerStatefulWidget {
  const DutyLocationFormScreen({super.key, this.existing, this.readPosition});
  final DutyLocationAdmin? existing;
  final CurrentPositionReader? readPosition;

  @override
  ConsumerState<DutyLocationFormScreen> createState() => _FormState();
}

class _FormState extends ConsumerState<DutyLocationFormScreen> {
  final _form = GlobalKey<FormState>();
  late final _name = TextEditingController(text: widget.existing?.name ?? '');
  late final _address = TextEditingController(text: widget.existing?.address ?? '');
  late final _lat = TextEditingController(
    text: widget.existing == null ? '' : '${roundCoord(widget.existing!.lat)}',
  );
  late final _lng = TextEditingController(
    text: widget.existing == null ? '' : '${roundCoord(widget.existing!.lng)}',
  );
  late final _radius = TextEditingController(text: '${widget.existing?.radiusM ?? 100}');
  final _paste = TextEditingController();
  late String _mode = widget.existing?.assignMode ?? 'selected';
  late bool _active = widget.existing?.isActive ?? true;
  final Map<String, String> _serverErrors = {};
  String? _pasteError;
  bool _locating = false;
  bool _saving = false;
  String? _error;

  @override
  void dispose() {
    for (final c in [_name, _address, _lat, _lng, _radius, _paste]) {
      c.dispose();
    }
    super.dispose();
  }

  void _applyPaste() {
    final p = parseCoordinates(_paste.text);
    if (p == null) {
      setState(() => _pasteError =
          'Could not find coordinates. Paste "12.97, 77.59" or a full maps link that contains them.',);
      return;
    }
    setState(() {
      _lat.text = '${roundCoord(p.lat)}';
      _lng.text = '${roundCoord(p.lng)}';
      _pasteError = null;
      _serverErrors
        ..remove('lat')
        ..remove('lng');
    });
  }

  Future<void> _useMyLocation() async {
    final read = widget.readPosition;
    if (read == null) return;
    setState(() {
      _locating = true;
      _pasteError = null;
    });
    try {
      final p = await read();
      if (!mounted) return;
      setState(() {
        _lat.text = '${roundCoord(p.lat)}';
        _lng.text = '${roundCoord(p.lng)}';
      });
    } on PositionReadException catch (e) {
      if (mounted) setState(() => _pasteError = e.message);
    } finally {
      if (mounted) setState(() => _locating = false);
    }
  }

  Future<void> _save() async {
    setState(() {
      _serverErrors.clear();
      _error = null;
    });
    if (!(_form.currentState?.validate() ?? false)) return;
    setState(() => _saving = true);
    try {
      await ref.read(onDutyRepositoryProvider).saveLocation(
            widget.existing?.id,
            dutyLocationBody(
              name: _name.text,
              address: _address.text,
              lat: _lat.text,
              lng: _lng.text,
              radius: _radius.text,
              assignMode: _mode,
              isActive: _active,
            ),
          );
      if (!mounted) return;
      showAdminSnack(context, widget.existing == null ? 'Location added.' : 'Location saved.');
      Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _saving = false;
        for (final d in e.details) {
          if (d.field.isNotEmpty) _serverErrors[d.field] = d.message.isEmpty ? 'Not accepted' : d.message;
        }
        if (e.code == 'CONFLICT' && !_serverErrors.containsKey('name')) {
          _serverErrors['name'] = 'A location with this name already exists';
        }
        _error = adminErrorMessage(e);
      });
      _form.currentState?.validate();
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final editing = widget.existing != null;
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: Text(editing ? 'Edit location' : 'Add location')),
        body: Form(
          key: _form,
          child: ListView(
            padding: const EdgeInsets.all(16),
            children: [
              TextFormField(
                controller: _name,
                enabled: !_saving,
                maxLength: 80,
                textInputAction: TextInputAction.next,
                decoration: const InputDecoration(labelText: 'Name', border: OutlineInputBorder()),
                validator: (v) => validateDutyName(v) ?? _serverErrors['name'],
              ),
              const SizedBox(height: 8),
              TextFormField(
                controller: _address,
                enabled: !_saving,
                maxLength: 300,
                maxLines: 2,
                minLines: 1,
                decoration: const InputDecoration(labelText: 'Address (optional)', border: OutlineInputBorder()),
                validator: (v) => validateDutyAddress(v) ?? _serverErrors['address'],
              ),
              const SizedBox(height: 8),
              Text('Position', style: theme.textTheme.titleSmall),
              const SizedBox(height: 8),
              TextField(
                controller: _paste,
                enabled: !_saving,
                decoration: InputDecoration(
                  labelText: 'Paste coordinates or a maps link',
                  helperText: 'For example 12.9716, 77.5946',
                  errorText: _pasteError,
                  border: const OutlineInputBorder(),
                  suffixIcon: IconButton(
                    tooltip: 'Use pasted coordinates',
                    icon: const Icon(Icons.check),
                    onPressed: _saving ? null : _applyPaste,
                  ),
                ),
                onSubmitted: (_) => _applyPaste(),
              ),
              const SizedBox(height: 12),
              LayoutBuilder(
                builder: (context, c) {
                  final narrow = c.maxWidth < 360;
                  final latField = TextFormField(
                    controller: _lat,
                    enabled: !_saving,
                    keyboardType: const TextInputType.numberWithOptions(decimal: true, signed: true),
                    decoration: const InputDecoration(labelText: 'Latitude', border: OutlineInputBorder()),
                    validator: (v) => validateLatitude(v) ?? _serverErrors['lat'],
                  );
                  final lngField = TextFormField(
                    controller: _lng,
                    enabled: !_saving,
                    keyboardType: const TextInputType.numberWithOptions(decimal: true, signed: true),
                    decoration: const InputDecoration(labelText: 'Longitude', border: OutlineInputBorder()),
                    validator: (v) => validateLongitude(v) ?? _serverErrors['lng'],
                  );
                  return narrow
                      ? Column(children: [latField, const SizedBox(height: 12), lngField])
                      : Row(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Expanded(child: latField),
                            const SizedBox(width: 12),
                            Expanded(child: lngField),
                          ],
                        );
                },
              ),
              if (widget.readPosition != null)
                Align(
                  alignment: Alignment.centerLeft,
                  child: TextButton.icon(
                    onPressed: _saving || _locating ? null : _useMyLocation,
                    icon: _locating
                        ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                        : const Icon(Icons.my_location),
                    label: const Text('Use my current location'),
                  ),
                ),
              const SizedBox(height: 12),
              TextFormField(
                controller: _radius,
                enabled: !_saving,
                keyboardType: TextInputType.number,
                decoration: const InputDecoration(
                  labelText: 'Radius (m)',
                  helperText: 'Allowed: 10 to 2000. Employees are never shown the radius.',
                  border: OutlineInputBorder(),
                ),
                validator: (v) => validateDutyRadius(v) ?? _serverErrors['radius_m'],
              ),
              const SizedBox(height: 16),
              Text('Who can use this location', style: theme.textTheme.titleSmall),
              const SizedBox(height: 8),
              SegmentedButton<String>(
                showSelectedIcon: false,
                segments: const [
                  ButtonSegment(value: 'selected', label: Text('Selected employees')),
                  ButtonSegment(value: 'all', label: Text('All employees')),
                ],
                selected: {_mode},
                onSelectionChanged: _saving ? null : (s) => setState(() => _mode = s.first),
              ),
              if (!editing)
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Text(
                    'Choose the employees after saving (Assignments).',
                    style: theme.textTheme.bodySmall,
                  ),
                ),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('Active'),
                subtitle: const Text('Inactive locations cannot be used for new check-ins or requests.'),
                value: _active,
                onChanged: _saving ? null : (v) => setState(() => _active = v),
              ),
              if (_error != null)
                Padding(
                  padding: const EdgeInsets.only(bottom: 8),
                  child: Semantics(
                    liveRegion: true,
                    child: Text(_error!, style: TextStyle(color: theme.colorScheme.error)),
                  ),
                ),
              FilledButton(
                onPressed: _saving ? null : _save,
                style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
                child: _saving
                    ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                    : const Text('Save'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
