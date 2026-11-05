import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_providers.dart' show departmentRefsProvider;
import '../domain/phase4a_models.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// Gratuity accrual (contract 18.3): an undiscounted estimate of what the
/// company would owe each employee under the Payment of Gratuity Act, as of a
/// date. It is never paid through a payslip and is NOT an actuarial valuation.
class GratuityScreen extends ConsumerStatefulWidget {
  const GratuityScreen({super.key});

  @override
  ConsumerState<GratuityScreen> createState() => _GratuityScreenState();
}

class _GratuityScreenState extends ConsumerState<GratuityScreen> {
  final _search = TextEditingController();
  Timer? _debounce;
  GratuityQuery _query = const GratuityQuery();
  int _page = 1;

  @override
  void dispose() {
    _debounce?.cancel();
    _search.dispose();
    super.dispose();
  }

  DateTime get _today => ref.read(payrollClockProvider)();

  DateTime get _asOf => parseYmd(_query.asOf) ?? _today;

  void _set(GratuityQuery q) => setState(() {
        _query = q;
        _page = 1;
      });

  Future<void> _pickDate() async {
    final today = _today;
    final d = await showDatePicker(
      context: context,
      initialDate: _asOf.isAfter(today) ? today : _asOf,
      firstDate: DateTime(2000),
      lastDate: DateTime(today.year, today.month, today.day),
      helpText: 'Gratuity as of',
    );
    if (d == null || !mounted) return;
    _set(GratuityQuery(
      asOf: ymd(d),
      departmentId: _query.departmentId,
      eligibleOnly: _query.eligibleOnly,
      q: _query.q,
    ),);
  }

  Future<void> _export() {
    final period = monthKey(_asOf);
    return downloadPayrollFile(context, ref, (repo) => repo.gratuityReport(period));
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final key = (_query, _page);
    final async = ref.watch(gratuityProvider(key));
    final depts = ref.watch(departmentRefsProvider).valueOrNull ?? const [];
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(
          title: const Text('Gratuity'),
          actions: [
            IconButton(
              key: const ValueKey('gratuity-export'),
              tooltip: 'Download Excel report for ${monthTitle(monthKey(_asOf))}',
              icon: const Icon(Icons.download_outlined),
              onPressed: _export,
            ),
          ],
        ),
        body: RefreshIndicator(
          onRefresh: () async => ref.invalidate(gratuityProvider(key)),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 48),
            children: [
              Text(
                'Accrued gratuity is an estimate for the books: 15/26 x last basic x years of service. '
                'It is paid separately, outside payroll, and is not an actuarial valuation.',
                style: theme.textTheme.bodySmall,
              ),
              const SizedBox(height: 8),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                crossAxisAlignment: WrapCrossAlignment.center,
                children: [
                  OutlinedButton.icon(
                    key: const ValueKey('gratuity-date'),
                    onPressed: _pickDate,
                    icon: const Icon(Icons.event_outlined),
                    label: Text('As of ${ymd(_asOf)}'),
                  ),
                  SizedBox(
                    width: 190,
                    child: DropdownButtonFormField<String?>(
                      key: const ValueKey('gratuity-dept'),
                      initialValue: depts.any((d) => d.id == _query.departmentId) ? _query.departmentId : null,
                      isExpanded: true,
                      decoration: const InputDecoration(
                        labelText: 'Department',
                        isDense: true,
                        border: OutlineInputBorder(),
                      ),
                      items: [
                        const DropdownMenuItem<String?>(child: Text('All')),
                        for (final d in depts) DropdownMenuItem<String?>(value: d.id, child: Text(d.name)),
                      ],
                      onChanged: (v) => _set(GratuityQuery(
                        asOf: _query.asOf,
                        departmentId: v,
                        eligibleOnly: _query.eligibleOnly,
                        q: _query.q,
                      ),),
                    ),
                  ),
                  FilterChip(
                    key: const ValueKey('gratuity-eligible'),
                    label: const Text('Eligible only'),
                    selected: _query.eligibleOnly,
                    onSelected: (v) => _set(GratuityQuery(
                      asOf: _query.asOf,
                      departmentId: _query.departmentId,
                      eligibleOnly: v,
                      q: _query.q,
                    ),),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              TextField(
                controller: _search,
                textInputAction: TextInputAction.search,
                decoration: const InputDecoration(
                  labelText: 'Search name or ID',
                  prefixIcon: Icon(Icons.search),
                  border: OutlineInputBorder(),
                  isDense: true,
                ),
                onChanged: (v) {
                  _debounce?.cancel();
                  _debounce = Timer(const Duration(milliseconds: 400), () {
                    if (!mounted) return;
                    _set(GratuityQuery(
                      asOf: _query.asOf,
                      departmentId: _query.departmentId,
                      eligibleOnly: _query.eligibleOnly,
                      q: v,
                    ),);
                  });
                },
              ),
              const SizedBox(height: 8),
              async.when(
                skipLoadingOnReload: true,
                loading: () => const Padding(padding: EdgeInsets.all(32), child: LoadingView(label: 'Loading gratuity')),
                error: (e, _) => ErrorView(message: payrollMsg(e), onRetry: () => ref.invalidate(gratuityProvider(key))),
                data: (g) => _body(g),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _body(GratuityPage g) {
    final theme = Theme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SectionCard(
          title: 'Totals for ${g.total} employees',
          children: [
            AmountRow(label: 'Accrued', value: formatBigRupees(g.accrued), key: const ValueKey('gratuity-total')),
            AmountRow(label: 'Accrued, eligible employees', value: formatBigRupees(g.eligibleAccrued)),
            AmountRow(label: 'Monthly accrual', value: formatBigRupees(g.monthlyAccrual)),
            Text(
              'Eligible after ${g.minYears} years of service. Limit ${formatBigRupees(g.cap)} per employee. '
              'Wage basis: ${g.basis == 'basic' ? 'basic' : 'basic, or half the gross if higher'}.',
              style: theme.textTheme.bodySmall,
            ),
          ],
        ),
        if (g.items.isEmpty)
          const Padding(
            padding: EdgeInsets.all(16),
            child: EmptyView(icon: Icons.savings_outlined, message: 'No employees match these filters.'),
          )
        else
          for (final i in g.items) _tile(i),
        if (g.total > g.pageSize)
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              IconButton(
                key: const ValueKey('gratuity-prev'),
                tooltip: 'Previous page',
                onPressed: _page > 1 ? () => setState(() => _page--) : null,
                icon: const Icon(Icons.chevron_left),
              ),
              Text('Page ${g.page}'),
              IconButton(
                key: const ValueKey('gratuity-next'),
                tooltip: 'Next page',
                onPressed: g.hasMore ? () => setState(() => _page++) : null,
                icon: const Icon(Icons.chevron_right),
              ),
            ],
          ),
      ],
    );
  }

  Widget _tile(GratuityItem i) {
    final theme = Theme.of(context);
    return Card(
      key: ValueKey('gratuity-${i.user.id}'),
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(child: Text(i.user.display, style: theme.textTheme.titleSmall)),
                Chip(
                  label: Text(i.eligible ? 'Eligible' : 'Not yet eligible'),
                  visualDensity: VisualDensity.compact,
                  side: BorderSide.none,
                  backgroundColor:
                      i.eligible ? theme.colorScheme.primaryContainer : theme.colorScheme.surfaceContainerHighest,
                ),
              ],
            ),
            Text(
              [
                if (i.joinDate != null) 'Joined ${i.joinDate}',
                if (i.exitDate != null) 'Leaves ${i.exitDate}',
                if (i.joinDate != null) 'Service ${i.serviceLabel} (counted ${i.countedYears} years)',
              ].join('  -  '),
              style: theme.textTheme.bodySmall,
            ),
            const SizedBox(height: 4),
            if (i.accrued == null)
              const Text('No amount')
            else ...[
              AmountRow(label: 'Accrued', value: formatBigRupees(i.accrued), bold: true),
              AmountRow(label: 'Last basic', value: formatBigRupees(i.wage)),
              AmountRow(label: 'Monthly accrual', value: formatBigRupees(i.monthlyAccrual)),
            ],
            if (i.flags.isNotEmpty)
              Wrap(
                spacing: 6,
                children: [
                  for (final f in i.flags)
                    Chip(
                      avatar: const Icon(Icons.warning_amber_outlined, size: 16),
                      label: Text(gratuityFlagLabel(f)),
                      visualDensity: VisualDensity.compact,
                    ),
                ],
              ),
          ],
        ),
      ),
    );
  }
}
