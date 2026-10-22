import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/widgets/state_views.dart';
import '../data/admin_repository.dart';
import '../domain/admin_models.dart';
import 'admin_providers.dart';
import 'admin_widgets.dart';

/// Typed settings form (`settings.manage`). Only changed keys are sent (PUT
/// merges). Every change is audited by the server.
class SettingsScreen extends ConsumerWidget {
  const SettingsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!perms.contains(AdminPerm.settingsManage)) {
      return const NoPermissionScreen(title: 'Settings');
    }
    final async = ref.watch(settingsProvider);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Settings')),
        body: async.when(
          loading: () => const LoadingView(),
          error: (e, _) => ErrorView(message: adminMsg(e), onRetry: () => ref.invalidate(settingsProvider)),
          data: (values) => SettingsForm(
            initial: values,
            onSave: (changes) async {
              try {
                await ref.read(adminRepositoryProvider).saveSettings(changes);
                ref.invalidate(settingsProvider);
                return null;
              } on ApiException catch (e) {
                return e;
              }
            },
          ),
        ),
      ),
    );
  }
}

/// Returns null on success or the [ApiException]; field errors from the
/// server (`details[].field` = setting key) are shown under the field.
class SettingsForm extends StatefulWidget {
  const SettingsForm({required this.initial, required this.onSave, super.key});
  final Map<String, Object?> initial;
  final Future<ApiException?> Function(Map<String, Object?> changes) onSave;

  @override
  State<SettingsForm> createState() => _SettingsFormState();
}

class _SettingsFormState extends State<SettingsForm> {
  final _form = GlobalKey<FormState>();
  final Map<String, TextEditingController> _text = {};
  final Map<String, bool> _bools = {};
  final Map<String, String> _serverErrors = {};
  bool _saving = false;
  String? _error;

  List<SettingSpec> get _specs =>
      [for (final s in settingSpecs) if (widget.initial.containsKey(s.key)) s];

  @override
  void initState() {
    super.initState();
    for (final s in _specs) {
      final v = widget.initial[s.key];
      if (s.kind == SettingKind.boolean) {
        _bools[s.key] = v == true;
      } else {
        _text[s.key] = TextEditingController(text: settingText(v));
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

  Future<void> _save() async {
    setState(() {
      _serverErrors.clear();
      _error = null;
    });
    if (!(_form.currentState?.validate() ?? false)) return;
    final edited = <String, Object?>{
      for (final s in _specs)
        s.key: s.kind == SettingKind.boolean
            ? _bools[s.key]
            : parseSettingValue(s, _text[s.key]!.text),
    };
    final changes = changedSettings(widget.initial, edited);
    if (changes.isEmpty) {
      showAdminSnack(context, 'Nothing changed.');
      return;
    }
    setState(() => _saving = true);
    final err = await widget.onSave(changes);
    if (!mounted) return;
    if (err == null) {
      setState(() => _saving = false);
      showAdminSnack(context, 'Settings saved.');
      return;
    }
    setState(() {
      _saving = false;
      for (final d in err.details) {
        if (d.field.isNotEmpty) _serverErrors[d.field] = d.message;
      }
      _error = adminErrorMessage(err);
    });
    _form.currentState?.validate();
  }

  @override
  Widget build(BuildContext context) {
    final specs = _specs;
    if (specs.isEmpty) {
      return const EmptyView(icon: Icons.settings_outlined, message: 'No editable settings were returned.');
    }
    final groups = <String, List<SettingSpec>>{};
    for (final s in specs) {
      groups.putIfAbsent(s.group, () => []).add(s);
    }
    return Form(
      key: _form,
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          for (final g in groups.entries) ...[
            Padding(
              padding: const EdgeInsets.only(top: 8, bottom: 8),
              child: Text(g.key, style: Theme.of(context).textTheme.titleMedium),
            ),
            for (final s in g.value)
              Padding(
                padding: const EdgeInsets.only(bottom: 12),
                child: s.kind == SettingKind.boolean
                    ? SwitchListTile(
                        contentPadding: EdgeInsets.zero,
                        title: Text(s.label),
                        value: _bools[s.key]!,
                        onChanged: (v) => setState(() => _bools[s.key] = v),
                      )
                    : TextFormField(
                        controller: _text[s.key],
                        keyboardType: s.kind == SettingKind.time
                            ? TextInputType.datetime
                            : const TextInputType.numberWithOptions(decimal: true, signed: true),
                        decoration: InputDecoration(
                          labelText: s.label,
                          helperText: s.hint ??
                              (s.min != null ? 'Allowed: ${s.min} to ${s.max}' : null),
                          border: const OutlineInputBorder(),
                        ),
                        validator: (v) => validateSetting(s, v) ?? _serverErrors[s.key],
                      ),
              ),
          ],
          if (_error != null)
            Text(_error!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
          const SizedBox(height: 12),
          FilledButton(
            onPressed: _saving ? null : _save,
            style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
            child: _saving
                ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('Save settings'),
          ),
        ],
      ),
    );
  }
}
