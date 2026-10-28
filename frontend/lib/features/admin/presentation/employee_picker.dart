import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../data/admin_repository.dart';
import '../domain/admin_models.dart';

/// Bottom sheet to search and pick one ACTIVE employee (`GET /admin/employees`,
/// so the server scope applies: managers only see their direct reports).
Future<Employee?> pickEmployee(BuildContext context, {String title = 'Choose employee'}) =>
    showModalBottomSheet<Employee>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (_) => FractionallySizedBox(heightFactor: 0.85, child: _EmployeePicker(title: title)),
    );

class _EmployeePicker extends ConsumerStatefulWidget {
  const _EmployeePicker({required this.title});
  final String title;

  @override
  ConsumerState<_EmployeePicker> createState() => _EmployeePickerState();
}

class _EmployeePickerState extends ConsumerState<_EmployeePicker> {
  String _q = '';
  late Future<List<Employee>> _items = _load();

  Future<List<Employee>> _load() async =>
      (await ref.read(adminRepositoryProvider).employees(EmployeeQuery(q: _q, status: 'active'))).items;

  @override
  Widget build(BuildContext context) => Padding(
        padding: EdgeInsets.only(bottom: MediaQuery.of(context).viewInsets.bottom),
        child: Column(
          children: [
            Text(widget.title, style: Theme.of(context).textTheme.titleMedium),
            Padding(
              padding: const EdgeInsets.all(12),
              child: TextField(
                autofocus: true,
                decoration: const InputDecoration(
                  prefixIcon: Icon(Icons.search),
                  labelText: 'Name or employee ID',
                  border: OutlineInputBorder(),
                ),
                onSubmitted: (v) => setState(() {
                  _q = v;
                  _items = _load();
                }),
              ),
            ),
            Expanded(
              child: FutureBuilder<List<Employee>>(
                future: _items,
                builder: (context, snap) {
                  if (snap.connectionState != ConnectionState.done) {
                    return const Center(child: CircularProgressIndicator());
                  }
                  if (snap.hasError) {
                    final e = snap.error;
                    return Center(child: Text(e is ApiException ? e.message : 'Could not load employees.'));
                  }
                  final list = snap.data ?? const <Employee>[];
                  if (list.isEmpty) return const Center(child: Text('No employees found.'));
                  return ListView(
                    children: [
                      for (final e in list)
                        ListTile(
                          leading: const Icon(Icons.person_outline),
                          title: Text(e.fullName.isEmpty ? e.employeeId : e.fullName),
                          subtitle: Text(e.employeeId),
                          onTap: () => Navigator.pop(context, e),
                        ),
                    ],
                  );
                },
              ),
            ),
          ],
        ),
      );
}
