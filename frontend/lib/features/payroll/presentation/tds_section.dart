import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/security/screen_protect.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart' show runWithReauth;
import '../data/payroll_repository.dart' show PayrollFile;
import '../domain/phase4a_models.dart' show formatBigRupees;
import '../domain/phase5b_models.dart' show fyOf, fyTitle;
import '../domain/phase5c_models.dart';
import '../domain/tds_computation.dart';
import 'payroll_providers.dart' show payrollClockProvider;
import 'payroll_widgets.dart';
import 'tax_providers.dart';
import 'tds_widgets.dart';
import 'tax_declarations_section.dart' show FyDropdown;

/// Filters of the summary: the flags HR acts on.
const List<String> _summaryFlags = [
  'TDS_OVERRIDDEN',
  'TDS_CAPPED_NET',
  'TDS_EXCESS',
  'NO_PAN_206AA',
  'RULES_CHANGED',
  'NO_DECLARATION',
];

/// TDS engine overview (contract 19.9): every employee's tax for the year, what has been deducted, what the
/// engine suggests for this month beside the payslip's figure. Needs `payroll.view_all`; the server checks
/// it again. Also the 24Q working-file export (password).
class TdsSection extends ConsumerStatefulWidget {
  const TdsSection({super.key});

  @override
  ConsumerState<TdsSection> createState() => _TdsSectionState();
}

class _TdsSectionState extends ConsumerState<TdsSection> {
  late String _fy = fyOf(ref.read(payrollClockProvider)());
  String _q = '';
  String? _flag;
  int _page = 1;

  TdsSummaryQuery get _query => TdsSummaryQuery(fy: _fy, q: _q, flag: _flag);

  void _reset({String? fy, String? q, String? flag, bool clearFlag = false}) => setState(() {
        _fy = fy ?? _fy;
        _q = q ?? _q;
        _flag = clearFlag ? null : (flag ?? _flag);
        _page = 1;
      });

  Future<void> _export24q() async {
    final pick = await showDialog<(String, int)>(
      context: context,
      builder: (_) => Tax24qDialog(initialFy: _fy, today: ref.read(payrollClockProvider)()),
    );
    if (pick == null || !mounted) return;
    await downloadPayrollFile(
      context,
      ref,
      (repo) async {
        PayrollFile? file;
        final done = await runWithReauth(
          context,
          reason: 'The 24Q working file contains PANs and needs your password. The download is recorded.',
          action: (pw) async => file = await repo.tax24q(fy: pick.$1, quarter: pick.$2, currentPassword: pw),
        );
        return done ? file : null;
      },
      describe: tax24qErrorMessage,
    );
  }

  @override
  Widget build(BuildContext context) {
    final key = (_query, _page);
    final async = ref.watch(tdsSummaryProvider(key));
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 0, 12, 4),
          child: Column(
            children: [
              Row(
                children: [
                  FyDropdown(value: _fy, fieldKey: 'tds-fy', onChanged: (v) => _reset(fy: v)),
                  const SizedBox(width: 12),
                  Expanded(
                    child: TextField(
                      key: const ValueKey('tds-search'),
                      decoration: const InputDecoration(
                        hintText: 'Search name or employee ID',
                        prefixIcon: Icon(Icons.search),
                        isDense: true,
                      ),
                      onSubmitted: (v) => _reset(q: v.trim()),
                    ),
                  ),
                ],
              ),
              Align(
                alignment: Alignment.centerLeft,
                child: TextButton.icon(
                  key: const ValueKey('tds-24q'),
                  onPressed: _export24q,
                  icon: const Icon(Icons.download_outlined),
                  label: const Text('24Q working file (Excel)'),
                ),
              ),
              SingleChildScrollView(
                scrollDirection: Axis.horizontal,
                child: Row(
                  children: [
                    Padding(
                      padding: const EdgeInsets.only(right: 8),
                      child: ChoiceChip(
                        key: const ValueKey('tds-filter-all'),
                        label: const Text('All'),
                        selected: _flag == null,
                        onSelected: (_) => _reset(clearFlag: true),
                      ),
                    ),
                    for (final f in _summaryFlags)
                      Padding(
                        padding: const EdgeInsets.only(right: 8),
                        child: ChoiceChip(
                          key: ValueKey('tds-filter-$f'),
                          label: Text(tdsSummaryFlagLabels[f] ?? f),
                          selected: _flag == f,
                          onSelected: (_) => _reset(flag: f),
                        ),
                      ),
                  ],
                ),
              ),
            ],
          ),
        ),
        Expanded(
          child: async.when(
            skipLoadingOnReload: true,
            loading: () => const LoadingView(label: 'Loading tax summary'),
            error: (e, _) => tdsUnavailable(e)
                ? EmptyView(icon: Icons.calculate_outlined, message: p5cMsg(e))
                : ErrorView(message: p5cMsg(e), onRetry: () => ref.invalidate(tdsSummaryProvider(key))),
            data: (page) => RefreshIndicator(
              onRefresh: () async => ref.invalidate(tdsSummaryProvider(key)),
              child: page.items.isEmpty
                  ? ListView(
                      physics: const AlwaysScrollableScrollPhysics(),
                      children: const [
                        SizedBox(height: 80),
                        EmptyView(icon: Icons.calculate_outlined, message: 'No employees match.'),
                      ],
                    )
                  : _list(page, key),
            ),
          ),
        ),
      ],
    );
  }

  Widget _list(TdsSummaryPage page, TdsSummaryKey key) {
    return ListView(
      physics: const AlwaysScrollableScrollPhysics(),
      padding: const EdgeInsets.fromLTRB(12, 4, 12, 48),
      children: [
        SectionCard(
          key: const ValueKey('tds-totals'),
          title: 'Totals for ${page.total} employee${page.total == 1 ? '' : 's'} in ${fyTitle(_fy)}',
          children: [
            AmountRow(label: 'Tax for the year', value: formatBigRupees(page.totalTax)),
            AmountRow(label: 'Deducted so far', value: formatBigRupees(page.tdsPaidSoFar)),
            AmountRow(label: 'Still to deduct', value: formatBigRupees(page.remainingTax), bold: true),
          ],
        ),
        for (final i in page.items) _tile(i),
        if (page.total > page.pageSize)
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              IconButton(
                key: const ValueKey('tds-prev'),
                tooltip: 'Previous page',
                onPressed: _page > 1 ? () => setState(() => _page--) : null,
                icon: const Icon(Icons.chevron_left),
              ),
              Text('Page ${page.page}'),
              IconButton(
                key: const ValueKey('tds-next'),
                tooltip: 'Next page',
                onPressed: page.hasMore ? () => setState(() => _page++) : null,
                icon: const Icon(Icons.chevron_right),
              ),
            ],
          ),
      ],
    );
  }

  Widget _tile(TdsSummaryItem i) {
    final theme = Theme.of(context);
    return Card(
      key: ValueKey('tds-${i.user.id}'),
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: InkWell(
        onTap: () async {
          await Navigator.of(context).push<void>(
            MaterialPageRoute<void>(
              builder: (_) => TdsPreviewScreen(userId: i.user.id, fy: _fy, name: i.user.display),
            ),
          );
        },
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(child: Text(i.user.display, style: theme.textTheme.titleSmall)),
                  Text(regimeLabel(i.regime), style: theme.textTheme.bodySmall),
                ],
              ),
              AmountRow(label: 'Tax for the year', value: formatBigRupees(i.totalTax)),
              AmountRow(label: 'Deducted so far', value: formatBigRupees(i.tdsPaidSoFar)),
              AmountRow(label: 'Still to deduct', value: formatBigRupees(i.remainingTax)),
              AmountRow(
                label: 'This month: computed',
                value: formatBigRupees(i.suggestedThisMonth),
                bold: true,
              ),
              AmountRow(
                label: 'This month: on the payslip',
                value: formatBigRupees(i.manualThisMonth),
                caption: i.differs ? 'Different from the computed figure' : null,
              ),
              if (i.flags.isNotEmpty)
                Wrap(
                  spacing: 6,
                  runSpacing: 4,
                  children: [
                    for (final f in i.flags)
                      Chip(
                        avatar: const Icon(Icons.warning_amber_outlined, size: 16),
                        label: Text(tdsSummaryFlagLabels[f] ?? tdsFlagLabel(f)),
                        visualDensity: VisualDensity.compact,
                      ),
                  ],
                ),
            ],
          ),
        ),
      ),
    );
  }
}

/// Financial year and quarter for the 24Q working file. Returns (fy, quarter) or null.
class Tax24qDialog extends StatefulWidget {
  const Tax24qDialog({required this.initialFy, required this.today, super.key});
  final String initialFy;
  final DateTime today;

  @override
  State<Tax24qDialog> createState() => _Tax24qDialogState();
}

class _Tax24qDialogState extends State<Tax24qDialog> {
  late String _fy = widget.initialFy;
  late int _quarter = quarterOf(widget.today);

  @override
  Widget build(BuildContext context) {
    final thisFy = fyOf(widget.today);
    final choices = {thisFy, widget.initialFy}.toList()..sort((a, b) => b.compareTo(a));
    return AlertDialog(
      title: const Text('24Q working file'),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text(
              'Working data for the return utility, from published payslips only. It is not a filing file. '
              'Months missing a PAN or a paid TDS challan are listed on the Blockers sheet.',
            ),
            const SizedBox(height: 12),
            DropdownButtonFormField<String>(
              key: const ValueKey('q24-fy'),
              initialValue: _fy,
              decoration: const InputDecoration(labelText: 'Financial year', border: OutlineInputBorder()),
              items: [for (final c in choices) DropdownMenuItem(value: c, child: Text(fyTitle(c)))],
              onChanged: (v) => setState(() => _fy = v ?? _fy),
            ),
            const SizedBox(height: 12),
            DropdownButtonFormField<int>(
              key: const ValueKey('q24-quarter'),
              initialValue: _quarter,
              isExpanded: true,
              decoration: const InputDecoration(labelText: 'Quarter', border: OutlineInputBorder()),
              items: [for (var q = 1; q <= 4; q++) DropdownMenuItem(value: q, child: Text(quarterLabel(q)))],
              onChanged: (v) => setState(() => _quarter = v ?? _quarter),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(
          key: const ValueKey('q24-continue'),
          onPressed: () => Navigator.pop(context, (_fy, _quarter)),
          style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
          child: const Text('Continue'),
        ),
      ],
    );
  }
}

/// One employee's tax worked out now (`POST /preview`, writes nothing): the figures, the other regime and the
/// computation statement PDF (password). Screenshot-protected.
class TdsPreviewScreen extends ConsumerStatefulWidget {
  const TdsPreviewScreen({required this.userId, required this.fy, required this.name, super.key});
  final String userId;
  final String fy;
  final String name;

  @override
  ConsumerState<TdsPreviewScreen> createState() => _TdsPreviewScreenState();
}

class _TdsPreviewScreenState extends ConsumerState<TdsPreviewScreen> {
  final _form = GlobalKey<FormState>();
  final _period = TextEditingController();
  String? _periodApplied;
  String? _regime;

  @override
  void dispose() {
    _period.dispose();
    super.dispose();
  }

  TdsPreviewKey get _key => (widget.userId, widget.fy, _periodApplied, _regime);

  void _apply() {
    if (!(_form.currentState?.validate() ?? false)) return;
    final t = _period.text.trim();
    setState(() => _periodApplied = t.isEmpty ? null : t);
  }

  Future<void> _statement() => downloadPayrollFile(
        context,
        ref,
        (repo) async {
          PayrollFile? file;
          final done = await runWithReauth(
            context,
            reason: 'The tax computation statement contains personal tax details and needs your password. '
                'The download is recorded.',
            action: (pw) async => file = await repo.taxStatement(widget.userId, fy: widget.fy, currentPassword: pw),
          );
          return done ? file : null;
        },
        describe: phase5cErrorMessage,
      );

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(tdsPreviewProvider(_key));
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(
          title: Text(widget.name, overflow: TextOverflow.ellipsis),
          actions: [
            IconButton(
              key: const ValueKey('tds-statement'),
              tooltip: 'Tax computation statement (PDF)',
              icon: const Icon(Icons.picture_as_pdf_outlined),
              onPressed: _statement,
            ),
          ],
        ),
        body: ListView(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 48),
          children: [
            Text(
              'Tax for ${fyTitle(widget.fy)} worked out now from the salary, the verified rules and the '
              'declaration. Nothing is saved. The statement is not Form 16.',
              style: Theme.of(context).textTheme.bodySmall,
            ),
            const SizedBox(height: 12),
            Form(
              key: _form,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  SegmentedButton<String?>(
                    key: const ValueKey('tds-regime'),
                    showSelectedIcon: false,
                    segments: const [
                      ButtonSegment(value: null, label: Text('As declared')),
                      ButtonSegment(value: 'old', label: Text('Old')),
                      ButtonSegment(value: 'new', label: Text('New')),
                    ],
                    selected: {_regime},
                    onSelectionChanged: (s) => setState(() => _regime = s.first),
                  ),
                  const SizedBox(height: 12),
                  TextFormField(
                    key: const ValueKey('tds-period'),
                    controller: _period,
                    keyboardType: TextInputType.datetime,
                    decoration: const InputDecoration(
                      labelText: 'Month (optional, YYYY-MM)',
                      helperText: 'Empty = the current month of the year',
                      border: OutlineInputBorder(),
                    ),
                    validator: (v) => validateTaxPeriod(v, widget.fy),
                    onFieldSubmitted: (_) => _apply(),
                  ),
                  const SizedBox(height: 8),
                  FilledButton.icon(
                    key: const ValueKey('tds-calculate'),
                    onPressed: _apply,
                    icon: const Icon(Icons.calculate_outlined),
                    label: const Text('Calculate'),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 12),
            async.when(
              skipLoadingOnReload: true,
              loading: () => const Padding(padding: EdgeInsets.all(32), child: LoadingView(label: 'Working out the tax')),
              error: (e, _) => tdsUnavailable(e)
                  ? EmptyView(icon: Icons.calculate_outlined, message: p5cMsg(e))
                  : ErrorView(message: p5cMsg(e), onRetry: () => ref.invalidate(tdsPreviewProvider(_key))),
              data: _result,
            ),
          ],
        ),
      ),
    );
  }

  Widget _result(TdsResult r) {
    final c = r.computation;
    final cmp = compareRegimes(c, r.otherRegime);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SectionCard(
          key: const ValueKey('tds-preview-card'),
          title: 'TDS for the month',
          children: [
            AmountRow(label: 'Computed TDS', value: formatBigRupees(c.suggested), bold: true),
            if (c.override != null)
              AmountRow(label: 'Set by hand on the payslip', value: formatBigRupees(c.override!.amount)),
            if (c.hasShortfall) AmountRow(label: 'Not deducted (pay too low)', value: formatBigRupees(c.shortfall)),
            for (final f in c.flags) TdsFlagTile(f),
          ],
        ),
        SectionCard(title: 'How it was worked out', children: [TdsFigures(computation: c)]),
        if (cmp != null) SectionCard(title: 'Old and new regime', children: [RegimeComparisonView(comparison: cmp)]),
      ],
    );
  }
}
