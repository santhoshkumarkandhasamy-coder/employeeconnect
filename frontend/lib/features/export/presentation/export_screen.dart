import 'dart:io';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/files/share_confirm.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/widgets/state_views.dart';
import '../../auth/presentation/auth_controller.dart';
import '../../auth/presentation/auth_state.dart';
import '../../food/data/food_repository.dart'
    show OpenResult, exportStoreProvider, fileOpenerProvider, shareConfirmedProvider;
import '../../food/presentation/food_providers.dart' show foodClockProvider;
import '../data/export_repository.dart';
import '../domain/export_models.dart';

/// Reports & Export (api-contract.md section 10). Shown only when the user has
/// `export.all|export.team` plus at least one per-sheet permission; the server
/// enforces all of it again. Reports contain employee names, so the screen is
/// screenshot-protected, the file lives only in the app-private export folder
/// and is deleted right after it was handed to the share sheet.
class ExportScreen extends ConsumerStatefulWidget {
  const ExportScreen({super.key});

  @override
  ConsumerState<ExportScreen> createState() => _ExportScreenState();
}

class _ExportScreenState extends ConsumerState<ExportScreen> {
  final _formKey = GlobalKey<FormState>();
  final _employee = TextEditingController();
  ExportType? _type;
  DateTime? _from;
  DateTime? _to;
  String? _departmentId;
  bool _includeReasons = false; // off by default (review 08 M2)
  bool _busy = false;
  double? _progress;
  bool _submitted = false;
  CancelToken? _cancel;

  @override
  void initState() {
    super.initState();
    final today = ref.read(foodClockProvider)();
    final p = exportPresets(today).first;
    _from = p.from;
    _to = p.to;
  }

  @override
  void dispose() {
    // Aborts a running download; the repository deletes the partial file.
    _cancel?.cancel('screen closed');
    _employee.dispose();
    super.dispose();
  }

  Future<void> _pickRange() async {
    final today = ref.read(foodClockProvider)();
    final picked = await showDateRangePicker(
      context: context,
      firstDate: DateTime(2000),
      lastDate: DateTime(today.year + 1, 12, 31),
      initialDateRange: _from != null && _to != null && !_to!.isBefore(_from!)
          ? DateTimeRange(start: _from!, end: _to!)
          : null,
    );
    if (picked != null) {
      setState(() {
        _from = picked.start;
        _to = picked.end;
      });
    }
  }

  Future<void> _run(ExportType type) async {
    setState(() => _submitted = true);
    if (_busy) return;
    if (validateExportRange(_from, _to) != null) return;
    if (!(_formKey.currentState?.validate() ?? false)) return;
    setState(() {
      _busy = true;
      _progress = null;
    });
    // Resolve everything before the first await: the screen may close while
    // the download runs and `ref` must not be used after that.
    final messenger = ScaffoldMessenger.of(context);
    final repo = ref.read(exportRepositoryProvider);
    final store = ref.read(exportStoreProvider);
    final opener = ref.read(fileOpenerProvider);
    final confirmed = ref.read(shareConfirmedProvider.notifier);
    final token = _cancel = CancelToken();
    // Only ever asked for when the user holds export.sensitive and the report
    // has reason columns; otherwise the server would answer 403.
    final perms = ref.read(authControllerProvider
        .select((s) => s is AuthAuthenticated ? s.user.permissions : const <String>[]),);
    final withReasons =
        _includeReasons && canExportReasons(perms) && exportTypeHasReasons(type);
    File? file;
    try {
      final r = await repo.download(
        type: type,
        from: _from!,
        to: _to!,
        store: store,
        departmentId: _departmentId,
        employeeId: _employee.text,
        includeReasons: withReasons,
        cancelToken: token,
        onProgress: (got, total) {
          if (mounted && total > 0) setState(() => _progress = got / total);
        },
      );
      file = r.file;
      if (!mounted) return; // finally deletes the file
      if (!await confirmShareOnce(context, confirmed)) {
        messenger.showSnackBar(const SnackBar(content: Text('Share cancelled. The file was deleted.')));
        return;
      }
      final result = await opener.open(file.path);
      if (!mounted) return;
      if (result == OpenResult.dismissed) {
        messenger.showSnackBar(const SnackBar(content: Text('Share cancelled. The file was deleted.')));
      } else if (result == OpenResult.unavailable) {
        messenger.showSnackBar(const SnackBar(
          content: Text('Sharing is not available on this device. The file was deleted.'),
        ),);
      }
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(
        content: Text(exportErrorMessage(e, includeReasons: withReasons)),
        duration: const Duration(seconds: 8),
      ),);
    } on DioException catch (e) {
      // Cancelled because the screen was closed: nothing to show.
      if (!CancelToken.isCancel(e)) {
        messenger.showSnackBar(const SnackBar(content: Text('Could not save the file.')));
      }
    } on Object {
      messenger.showSnackBar(const SnackBar(content: Text('Could not save the file.')));
    } finally {
      // The share target has its own copy; ours is never kept.
      if (file != null) _deleteQuietly(file);
      if (mounted) setState(() => _busy = false);
    }
  }

  void _deleteQuietly(File f) {
    try {
      if (f.existsSync()) f.deleteSync();
    } on Object {
      // The folder is also wiped on logout, lock and next start.
    }
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(authControllerProvider
        .select((s) => s is AuthAuthenticated ? s.user.permissions : const <String>[]),);
    final types = allowedExportTypes(perms);
    final type = types.contains(_type) ? _type! : (types.isEmpty ? null : types.first);
    final today = ref.read(foodClockProvider)();
    final rangeError = _submitted || _from != null ? validateExportRange(_from, _to) : null;
    final departments = ref.watch(exportDepartmentsProvider).valueOrNull ?? const [];

    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Reports & Export')),
        body: type == null
            ? const EmptyView(
                message: 'You do not have access to reports.',
                icon: Icons.lock_outline,
              )
            : Form(
                key: _formKey,
                child: ListView(
                  padding: const EdgeInsets.all(16),
                  children: [
                    Text('Report', style: Theme.of(context).textTheme.titleSmall),
                    const SizedBox(height: 4),
                    RadioGroupLike(
                      types: types,
                      value: type,
                      enabled: !_busy,
                      onChanged: (t) => setState(() => _type = t),
                    ),
                    const SizedBox(height: 16),
                    Text('Dates', style: Theme.of(context).textTheme.titleSmall),
                    const SizedBox(height: 8),
                    Wrap(
                      spacing: 8,
                      runSpacing: 4,
                      children: [
                        for (final p in exportPresets(today))
                          ActionChip(
                            label: Text(p.label),
                            onPressed: _busy
                                ? null
                                : () => setState(() {
                                      _from = p.from;
                                      _to = p.to;
                                    }),
                          ),
                      ],
                    ),
                    ListTile(
                      contentPadding: EdgeInsets.zero,
                      leading: const Icon(Icons.date_range),
                      title: Text(
                        _from == null || _to == null
                            ? 'Choose dates'
                            : exportRangeLabel(_from!, _to!),
                      ),
                      subtitle: Text(
                        rangeError ?? 'Up to $exportMaxDays days. Tap to change.',
                        style: rangeError == null
                            ? null
                            : TextStyle(color: Theme.of(context).colorScheme.error),
                      ),
                      trailing: const Icon(Icons.expand_more),
                      onTap: _busy ? null : _pickRange,
                    ),
                    if (departments.isNotEmpty) ...[
                      const SizedBox(height: 8),
                      DropdownButtonFormField<String?>(
                        initialValue: _departmentId,
                        isExpanded: true,
                        decoration: const InputDecoration(
                          labelText: 'Department (optional)',
                          border: OutlineInputBorder(),
                        ),
                        items: [
                          const DropdownMenuItem<String?>(
                            value: null,
                            child: Text('All departments'),
                          ),
                          for (final d in departments)
                            DropdownMenuItem<String?>(
                              value: d.id,
                              child: Text(d.name, overflow: TextOverflow.ellipsis),
                            ),
                        ],
                        onChanged: _busy ? null : (v) => setState(() => _departmentId = v),
                      ),
                    ],
                    const SizedBox(height: 16),
                    TextFormField(
                      controller: _employee,
                      enabled: !_busy,
                      textInputAction: TextInputAction.done,
                      autocorrect: false,
                      enableSuggestions: false,
                      maxLength: 32,
                      decoration: const InputDecoration(
                        labelText: 'Employee ID (optional)',
                        helperText: 'For example EMP001',
                        border: OutlineInputBorder(),
                      ),
                      validator: validateEmployeeIdFilter,
                    ),
                    if (canExportReasons(perms) && exportTypeHasReasons(type)) ...[
                      const SizedBox(height: 8),
                      SwitchListTile(
                        key: const ValueKey('include-reasons'),
                        contentPadding: EdgeInsets.zero,
                        title: const Text('Include reasons and comments'),
                        subtitle: const Text(
                          'Adds the Reason and Comment columns to the Leave and '
                          'Permission sheets. Reasons may contain health or '
                          'other private details, so share the file only with people who need '
                          'it. Your export is logged.',
                        ),
                        value: _includeReasons,
                        onChanged: _busy ? null : (v) => setState(() => _includeReasons = v),
                      ),
                    ],
                    const SizedBox(height: 8),
                    if (_busy) ...[
                      LinearProgressIndicator(value: _progress),
                      const SizedBox(height: 8),
                      const Text('Creating the report...'),
                    ] else
                      FilledButton.icon(
                        style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
                        onPressed: () => _run(type),
                        icon: const Icon(Icons.download_outlined),
                        label: Text('Export ${type.label}'),
                      ),
                    const SizedBox(height: 12),
                    Text(
                      'The Excel file is kept only in private app storage and is deleted '
                      'after sharing. Personal details such as phone numbers and '
                      'locations are never included. Leave and Permission sheets have no '
                      'reason columns unless reasons are included above.'
                      '${isTeamScopedExport(perms) ? ' Team reports cover your direct reports and may only reach back about 12 months.' : ''}',
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                  ],
                ),
              ),
      ),
    );
  }
}

/// Simple selectable list of report types (avoids the RadioListTile API that
/// changed between Flutter versions).
class RadioGroupLike extends StatelessWidget {
  const RadioGroupLike({
    required this.types,
    required this.value,
    required this.onChanged,
    this.enabled = true,
    super.key,
  });

  final List<ExportType> types;
  final ExportType value;
  final ValueChanged<ExportType> onChanged;
  final bool enabled;

  @override
  Widget build(BuildContext context) => Column(
        children: [
          for (final t in types)
            ListTile(
              contentPadding: EdgeInsets.zero,
              dense: true,
              selected: t == value,
              leading: Icon(t == value ? Icons.radio_button_checked : Icons.radio_button_off),
              title: Text(t.label),
              subtitle: Text(t.description),
              enabled: enabled,
              onTap: () => onChanged(t),
            ),
        ],
      );
}
