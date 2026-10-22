import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/widgets/state_views.dart';
import '../data/admin_repository.dart';
import '../domain/admin_models.dart';
import 'admin_providers.dart';
import 'admin_widgets.dart';

/// Per-employee leave quota adjustment (`leave_balance.manage`).
class LeaveBalancesScreen extends ConsumerStatefulWidget {
  const LeaveBalancesScreen({required this.employee, super.key, this.year});
  final Employee employee;
  final int? year;

  @override
  ConsumerState<LeaveBalancesScreen> createState() => _LeaveBalancesScreenState();
}

class _LeaveBalancesScreenState extends ConsumerState<LeaveBalancesScreen> {
  late int _year = widget.year ?? DateTime.now().year;
  final Map<String, double> _edited = {};
  bool _saving = false;

  BalanceKey get _key => (widget.employee.id, _year);

  Future<void> _editQuota(LeaveBalanceRow r) async {
    final ctl = TextEditingController(text: '${_edited[r.leaveTypeId] ?? r.quota}');
    final formKey = GlobalKey<FormState>();
    final v = await showDialog<double>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(r.label),
        content: Form(
          key: formKey,
          child: TextFormField(
            controller: ctl,
            autofocus: true,
            keyboardType: const TextInputType.numberWithOptions(decimal: true),
            decoration: InputDecoration(
              labelText: 'Quota (days)',
              helperText: 'At least ${r.minQuota} (used + pending)',
            ),
            validator: (s) => validateQuota(s, r.minQuota),
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
          FilledButton(
            onPressed: () {
              if (formKey.currentState?.validate() ?? false) {
                Navigator.pop(ctx, double.parse(ctl.text.trim()));
              }
            },
            child: const Text('Set'),
          ),
        ],
      ),
    );
    ctl.dispose();
    if (v != null) setState(() => _edited[r.leaveTypeId] = v);
  }

  Future<void> _save(List<LeaveBalanceRow> rows) async {
    setState(() => _saving = true);
    try {
      await ref.read(adminRepositoryProvider).saveLeaveBalances(
        widget.employee.id,
        _year,
        [
          for (final r in rows)
            if (_edited.containsKey(r.leaveTypeId)) r.withQuota(_edited[r.leaveTypeId]!),
        ],
      );
      _edited.clear();
      ref.invalidate(leaveBalancesProvider(_key));
      if (mounted) showAdminSnack(context, 'Leave balances saved.');
    } on ApiException catch (e) {
      if (mounted) showAdminSnack(context, adminErrorMessage(e));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!perms.contains(AdminPerm.leaveBalanceManage)) {
      return const NoPermissionScreen(title: 'Leave balances');
    }
    final async = ref.watch(leaveBalancesProvider(_key));
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(
          title: Text('Leave balances $_year'),
          actions: [
            IconButton(
              tooltip: 'Previous year',
              icon: const Icon(Icons.chevron_left),
              onPressed: () => setState(() {
                _year--;
                _edited.clear();
              }),
            ),
            IconButton(
              tooltip: 'Next year',
              icon: const Icon(Icons.chevron_right),
              onPressed: () => setState(() {
                _year++;
                _edited.clear();
              }),
            ),
          ],
        ),
        body: async.when(
          loading: () => const LoadingView(),
          error: (e, _) => ErrorView(
            message: adminMsg(e),
            onRetry: () => ref.invalidate(leaveBalancesProvider(_key)),
          ),
          data: (rows) => rows.isEmpty
              ? const EmptyView(
                  icon: Icons.beach_access_outlined,
                  message: 'No leave types are set up for this year.',
                )
              : Column(
                  children: [
                    Expanded(
                      child: ListView(
                        children: [
                          for (final r in rows)
                            ListTile(
                              title: Text(r.label),
                              subtitle: Text('Used ${r.used}, pending ${r.pending}'),
                              trailing: Text(
                                '${_edited[r.leaveTypeId] ?? r.quota} days',
                                style: TextStyle(
                                  fontWeight: _edited.containsKey(r.leaveTypeId)
                                      ? FontWeight.bold
                                      : FontWeight.normal,
                                ),
                              ),
                              onTap: () => _editQuota(r),
                            ),
                        ],
                      ),
                    ),
                    SafeArea(
                      child: Padding(
                        padding: const EdgeInsets.all(12),
                        child: FilledButton(
                          onPressed: _edited.isEmpty || _saving ? null : () => _save(rows),
                          style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
                          child: const Text('Save changes'),
                        ),
                      ),
                    ),
                  ],
                ),
        ),
      ),
    );
  }
}

/// Quota must be a number between [min] (used + pending) and 366.
String? validateQuota(String? raw, double min) {
  final n = double.tryParse((raw ?? '').trim());
  if (n == null || n.isNaN || n.isInfinite) return 'Enter a number';
  if (n < min) return 'Cannot be below $min (used + pending)';
  if (n > 366) return 'Cannot be more than 366';
  return null;
}
