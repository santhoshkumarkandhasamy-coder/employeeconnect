import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/security/screen_protect.dart';
import '../domain/admin_models.dart';
import 'admin_providers.dart';
import 'admin_widgets.dart';
import 'employee_detail_screen.dart';
import 'employee_form_screen.dart';

/// Employee list with server-side search and filters (needs `employee.view`).
/// Contains personal data, so it is a [SecureScreen].
class EmployeesScreen extends ConsumerStatefulWidget {
  const EmployeesScreen({super.key, this.pickMode = false, this.excludeId});

  /// Pick one employee (manager picker): tapping returns it via `Navigator.pop`.
  final bool pickMode;
  final String? excludeId;

  @override
  ConsumerState<EmployeesScreen> createState() => _EmployeesScreenState();
}

class _EmployeesScreenState extends ConsumerState<EmployeesScreen> {
  final _search = TextEditingController();
  Timer? _debounce;
  EmployeeQuery _query = const EmployeeQuery();

  @override
  void dispose() {
    _debounce?.cancel();
    _search.dispose();
    super.dispose();
  }

  void _onSearch(String v) {
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 400), () {
      if (mounted) {
        setState(() => _query = EmployeeQuery(
              q: v,
              departmentId: _query.departmentId,
              status: _query.status,
            ),);
      }
    });
  }

  Future<void> _create() async {
    final created = await Navigator.of(context).push<bool>(
      MaterialPageRoute<bool>(builder: (_) => const EmployeeFormScreen()),
    );
    if (created == true) ref.invalidate(employeesProvider(_query));
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!perms.contains(AdminPerm.employeeView)) {
      return const NoPermissionScreen(title: 'Employees');
    }
    final depts = ref.watch(departmentRefsProvider).valueOrNull ?? const <NamedRef>[];
    final async = ref.watch(employeesProvider(_query));
    final notifier = ref.read(employeesProvider(_query).notifier);

    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: Text(widget.pickMode ? 'Choose employee' : 'Employees')),
        floatingActionButton: !widget.pickMode && perms.contains(AdminPerm.employeeCreate)
            ? FloatingActionButton.extended(
                onPressed: _create,
                icon: const Icon(Icons.person_add_alt_1),
                label: const Text('Add'),
              )
            : null,
        body: AdminPagedBody<Employee>(
          async: async,
          onRefresh: notifier.reload,
          onLoadMore: notifier.loadMore,
          emptyIcon: Icons.people_outline,
          emptyMessage: _query.q.isNotEmpty || _query.status != null || _query.departmentId != null
              ? 'No employees match these filters.'
              : 'No employees yet.',
          header: Padding(
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 0),
            child: Column(
              children: [
                TextField(
                  controller: _search,
                  onChanged: _onSearch,
                  textInputAction: TextInputAction.search,
                  decoration: const InputDecoration(
                    labelText: 'Search name, ID or email',
                    prefixIcon: Icon(Icons.search),
                    border: OutlineInputBorder(),
                    isDense: true,
                  ),
                ),
                const SizedBox(height: 8),
                SingleChildScrollView(
                  scrollDirection: Axis.horizontal,
                  child: Row(
                    children: [
                      for (final s in const <(String?, String)>[
                        (null, 'All'),
                        ('active', 'Active'),
                        ('inactive', 'Inactive'),
                      ])
                        Padding(
                          padding: const EdgeInsets.only(right: 8),
                          child: ChoiceChip(
                            label: Text(s.$2),
                            selected: _query.status == s.$1,
                            onSelected: (_) => setState(() => _query = EmployeeQuery(
                                  q: _query.q,
                                  departmentId: _query.departmentId,
                                  status: s.$1,
                                ),),
                          ),
                        ),
                      if (depts.isNotEmpty)
                        PopupMenuButton<String?>(
                          tooltip: 'Filter by department',
                          onSelected: (v) => setState(() => _query = EmployeeQuery(
                                q: _query.q,
                                departmentId: v == '' ? null : v,
                                status: _query.status,
                              ),),
                          itemBuilder: (_) => [
                            const PopupMenuItem(value: '', child: Text('All departments')),
                            for (final d in depts) PopupMenuItem(value: d.id, child: Text(d.name)),
                          ],
                          child: Chip(
                            avatar: const Icon(Icons.apartment_outlined, size: 18),
                            label: Text(
                              depts.where((d) => d.id == _query.departmentId).firstOrNull?.name ??
                                  'Department',
                            ),
                          ),
                        ),
                    ],
                  ),
                ),
              ],
            ),
          ),
          itemBuilder: (context, e) {
            if (e.id == widget.excludeId) return const SizedBox.shrink();
            return Card(
              margin: const EdgeInsets.symmetric(vertical: 4),
              child: ListTile(
                leading: CircleAvatar(child: Text(_initial(e))),
                title: Text(e.fullName.isEmpty ? e.employeeId : e.fullName,
                    maxLines: 1, overflow: TextOverflow.ellipsis,),
                subtitle: Text(
                  [e.employeeId, if (e.department != null) e.department!.name, if (e.role != null) e.role!.name]
                      .join('  -  '),
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
                trailing: e.isActive
                    ? (e.isLocked ? const Icon(Icons.lock_outline) : null)
                    : const Chip(label: Text('Inactive')),
                onTap: () async {
                  if (widget.pickMode) {
                    Navigator.of(context).pop(e);
                    return;
                  }
                  await Navigator.of(context).push<void>(
                    MaterialPageRoute<void>(builder: (_) => EmployeeDetailScreen(employeeId: e.id)),
                  );
                  ref.invalidate(employeesProvider(_query));
                },
              ),
            );
          },
        ),
      ),
    );
  }

  static String _initial(Employee e) {
    final n = e.fullName.isEmpty ? e.employeeId : e.fullName;
    return n.isEmpty ? '?' : n.substring(0, 1).toUpperCase();
  }
}
