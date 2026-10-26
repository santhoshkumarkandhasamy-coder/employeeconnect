import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../core/api/api_exception.dart';
import '../../../../core/security/screen_protect.dart';
import '../../../../core/widgets/state_views.dart';
import '../../../admin/domain/admin_models.dart' show NamedRef, adminErrorMessage;
import '../../../admin/presentation/admin_providers.dart' show adminMsg, departmentRefsProvider;
import '../../../admin/presentation/admin_widgets.dart' show showAdminSnack;
import '../../data/on_duty_repository.dart';
import '../../domain/duty_admin_models.dart';
import 'duty_admin_providers.dart';

/// Who may use a location: everyone, or a chosen list. "Add department"
/// adds that department's CURRENT active members when saved (the server
/// expands it; no department link is stored).
class DutyAssignmentsScreen extends ConsumerWidget {
  const DutyAssignmentsScreen({required this.location, super.key});
  final DutyLocationAdmin location;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(dutyAssignmentsProvider(location.id));
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: Text('Assignments: ${location.name}')),
        body: async.when(
          loading: () => const LoadingView(),
          error: (e, _) => ErrorView(
            message: adminMsg(e),
            onRetry: () => ref.invalidate(dutyAssignmentsProvider(location.id)),
          ),
          data: (a) => AssignmentsEditor(location: location, initial: a),
        ),
      ),
    );
  }
}

class AssignmentsEditor extends ConsumerStatefulWidget {
  const AssignmentsEditor({required this.location, required this.initial, super.key});
  final DutyLocationAdmin location;
  final DutyAssignments initial;

  @override
  ConsumerState<AssignmentsEditor> createState() => _EditorState();
}

class _EditorState extends ConsumerState<AssignmentsEditor> {
  late AssignmentSelection _sel = AssignmentSelection(
    mode: widget.initial.assignMode,
    users: widget.initial.users,
  );
  final _search = TextEditingController();
  final _filter = TextEditingController();
  String _query = '';
  bool _saving = false;
  String? _error;

  @override
  void dispose() {
    _search.dispose();
    _filter.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    final problem = _sel.problem;
    if (problem != null) {
      setState(() => _error = problem);
      return;
    }
    setState(() {
      _saving = true;
      _error = null;
    });
    try {
      await ref.read(onDutyRepositoryProvider).saveAssignments(widget.location.id, _sel);
      ref.invalidate(dutyAssignmentsProvider(widget.location.id));
      if (!mounted) return;
      showAdminSnack(context, 'Assignments saved.');
      Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _saving = false;
        _error = adminErrorMessage(e);
      });
    }
  }

  Future<void> _pickDepartments(List<NamedRef> options) async {
    final chosen = await showDialog<List<String>>(
      context: context,
      builder: (_) => _DepartmentDialog(options: options, selected: _sel.departmentIds),
    );
    if (chosen == null) return;
    setState(() {
      var s = _sel;
      for (final id in {..._sel.departmentIds, ...chosen}) {
        if (_sel.departmentIds.contains(id) != chosen.contains(id)) s = s.toggleDepartment(id);
      }
      _sel = s;
    });
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final depts = ref.watch(departmentRefsProvider);
    final results = _query.trim().length >= 2 ? ref.watch(dutyEmployeeSearchProvider(_query.trim())) : null;
    final shown = _sel.filtered(_filter.text);
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        SegmentedButton<String>(
          showSelectedIcon: false,
          segments: const [
            ButtonSegment(value: 'selected', label: Text('Selected employees')),
            ButtonSegment(value: 'all', label: Text('All employees')),
          ],
          selected: {_sel.mode},
          onSelectionChanged: _saving ? null : (s) => setState(() => _sel = _sel.withMode(s.first)),
        ),
        const SizedBox(height: 8),
        Text(
          _sel.isAll
              ? 'Every active employee can use this location. The list below is kept, so switching '
                  'back to selected employees restores it.'
              : 'Only the employees below can use this location.',
          style: theme.textTheme.bodySmall,
        ),
        const SizedBox(height: 16),
        Text('Add employees', style: theme.textTheme.titleSmall),
        const SizedBox(height: 8),
        TextField(
          controller: _search,
          enabled: !_saving,
          decoration: const InputDecoration(
            labelText: 'Search by name or ID',
            prefixIcon: Icon(Icons.search),
            border: OutlineInputBorder(),
          ),
          onChanged: (v) => setState(() => _query = v),
        ),
        if (results != null)
          results.when(
            loading: () => const Padding(padding: EdgeInsets.all(12), child: LinearProgressIndicator()),
            error: (e, _) => Padding(
              padding: const EdgeInsets.all(8),
              child: Text(adminMsg(e), style: TextStyle(color: theme.colorScheme.error)),
            ),
            data: (list) => list.isEmpty
                ? const Padding(padding: EdgeInsets.all(12), child: Text('No employees match.'))
                : Column(
                    children: [
                      for (final u in list)
                        CheckboxListTile(
                          dense: true,
                          value: _sel.contains(u.id),
                          title: Text(u.display, overflow: TextOverflow.ellipsis),
                          onChanged: _saving ? null : (_) => setState(() => _sel = _sel.toggle(u)),
                        ),
                      TextButton(
                        onPressed: _saving ? null : () => setState(() => _sel = _sel.addAll(list)),
                        child: const Text('Add all results'),
                      ),
                    ],
                  ),
          ),
        const SizedBox(height: 16),
        Text('Add a department', style: theme.textTheme.titleSmall),
        const SizedBox(height: 4),
        Text(
          "Adds the department's active members when you save. Employees who join later are not added.",
          style: theme.textTheme.bodySmall,
        ),
        const SizedBox(height: 8),
        depts.when(
          loading: () => const LinearProgressIndicator(),
          error: (e, _) => Text(adminMsg(e), style: TextStyle(color: theme.colorScheme.error)),
          data: (list) {
            final byId = {for (final d in list) d.id: d.name};
            return Wrap(
              spacing: 8,
              runSpacing: 4,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                for (final id in _sel.departmentIds)
                  InputChip(
                    label: Text(byId[id] ?? 'Department'),
                    onDeleted: _saving ? null : () => setState(() => _sel = _sel.toggleDepartment(id)),
                  ),
                OutlinedButton.icon(
                  onPressed: _saving || list.isEmpty ? null : () => _pickDepartments(list),
                  icon: const Icon(Icons.apartment_outlined),
                  label: const Text('Add department'),
                ),
              ],
            );
          },
        ),
        const Divider(height: 32),
        Row(
          children: [
            Expanded(child: Text('Assigned (${_sel.count})', style: theme.textTheme.titleSmall)),
            if (_sel.count > 0)
              TextButton(
                onPressed: _saving ? null : () => setState(() => _sel = _sel.clear()),
                child: const Text('Remove all'),
              ),
          ],
        ),
        if (_sel.count > 8)
          TextField(
            controller: _filter,
            decoration: const InputDecoration(
              labelText: 'Filter assigned',
              prefixIcon: Icon(Icons.filter_list),
              border: OutlineInputBorder(),
            ),
            onChanged: (_) => setState(() {}),
          ),
        if (_sel.count == 0)
          const Padding(
            padding: EdgeInsets.all(12),
            child: Text('No employees chosen yet.'),
          )
        else
          for (final u in shown)
            ListTile(
              dense: true,
              contentPadding: EdgeInsets.zero,
              title: Text(u.display, overflow: TextOverflow.ellipsis),
              trailing: IconButton(
                tooltip: 'Remove ${u.fullName.isEmpty ? u.employeeId : u.fullName}',
                icon: const Icon(Icons.close),
                onPressed: _saving ? null : () => setState(() => _sel = _sel.remove(u.id)),
              ),
            ),
        if (_error != null)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 8),
            child: Semantics(
              liveRegion: true,
              child: Text(_error!, style: TextStyle(color: theme.colorScheme.error)),
            ),
          ),
        const SizedBox(height: 8),
        FilledButton(
          onPressed: _saving ? null : _save,
          style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
          child: _saving
              ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
              : const Text('Save assignments'),
        ),
      ],
    );
  }
}

class _DepartmentDialog extends StatefulWidget {
  const _DepartmentDialog({required this.options, required this.selected});
  final List<NamedRef> options;
  final List<String> selected;

  @override
  State<_DepartmentDialog> createState() => _DepartmentDialogState();
}

class _DepartmentDialogState extends State<_DepartmentDialog> {
  late final Set<String> _chosen = {...widget.selected};

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: const Text('Add departments'),
        content: SizedBox(
          width: double.maxFinite,
          child: ListView(
            shrinkWrap: true,
            children: [
              for (final d in widget.options)
                CheckboxListTile(
                  dense: true,
                  value: _chosen.contains(d.id),
                  title: Text(d.name),
                  onChanged: (v) => setState(() => v == true ? _chosen.add(d.id) : _chosen.remove(d.id)),
                ),
            ],
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, _chosen.toList()), child: const Text('Done')),
        ],
      );
}
