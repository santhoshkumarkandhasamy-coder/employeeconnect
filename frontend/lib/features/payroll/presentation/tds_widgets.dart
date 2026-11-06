import 'package:flutter/material.dart';

import '../domain/payroll_models.dart' show Payslip;
import '../domain/phase4a_models.dart' show formatBigRupees;
import '../domain/phase5c_models.dart';
import '../domain/tds_computation.dart';
import 'payroll_widgets.dart';

/// Pieces of the TDS engine screens (contract 19.9, 19.16): the payslip card, the worked-out figures and
/// the regime comparison. Read-only; the amounts are shown, never logged.

/// A flag with its sentence.
class TdsFlagTile extends StatelessWidget {
  const TdsFlagTile(this.flag, {super.key});
  final String flag;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.only(top: 6),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(Icons.info_outline, size: 18, color: theme.colorScheme.tertiary),
          const SizedBox(width: 8),
          Expanded(
            child: Text.rich(
              TextSpan(
                children: [
                  TextSpan(text: '${tdsFlagLabel(flag)}. ', style: const TextStyle(fontWeight: FontWeight.w600)),
                  TextSpan(text: tdsFlagExplanation(flag)),
                ],
              ),
              style: theme.textTheme.bodySmall,
            ),
          ),
        ],
      ),
    );
  }
}

/// How the figure was worked out: the annual numbers and what has been deducted so far.
class TdsFigures extends StatelessWidget {
  const TdsFigures({required this.computation, super.key, this.showSteps = true});
  final TdsComputation computation;
  final bool showSteps;

  @override
  Widget build(BuildContext context) {
    final c = computation;
    final theme = Theme.of(context);
    final steps = showSteps ? c.steps : null;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          '${regimeLabel(c.regime)}${c.regimeIsDefault ? ' (default: nothing chosen yet)' : ''}  -  '
          '${c.remainingMonths} month${c.remainingMonths == 1 ? '' : 's'} left in ${c.fy}  -  '
          '${proofBasisLabel(c.proofBasis)} counted',
          style: theme.textTheme.bodySmall,
        ),
        const SizedBox(height: 4),
        for (final r in TdsAnnual.rows)
          if (c.annual[r.$1] != null)
            AmountRow(
              key: ValueKey('tds-annual-${r.$1}'),
              label: r.$2,
              value: formatBigRupees(c.annual[r.$1]),
              bold: r.$1 == 'taxable_income' || r.$1 == 'total_tax',
            ),
        const Divider(),
        AmountRow(label: 'Tax deducted so far this year', value: formatBigRupees(c.tdsPaidSoFar)),
        if (c.previousEmployerTds != '0.00')
          AmountRow(label: 'Tax deducted by a previous employer', value: formatBigRupees(c.previousEmployerTds)),
        AmountRow(label: 'Tax still to deduct', value: formatBigRupees(c.remainingTax)),
        if (c.oneTimeTax != '0.00')
          AmountRow(
            label: 'Extra tax on one-time earnings this month',
            value: formatBigRupees(c.oneTimeTax),
          ),
        if (steps != null && steps.sections.isNotEmpty) ...[
          const SizedBox(height: 4),
          Text('Deduction sections counted', style: theme.textTheme.titleSmall),
          for (final s in steps.sections)
            AmountRow(
              label: s.section,
              value: formatBigRupees(s.allowed),
              caption: 'Declared or verified ${formatBigRupees(s.counted)}',
            ),
        ],
        if (steps?.hraExemption != null && steps!.hraExemption != '0.00')
          AmountRow(label: 'HRA exemption', value: formatBigRupees(steps.hraExemption)),
      ],
    );
  }
}

/// Total tax under the regime in use against the other regime.
class RegimeComparisonView extends StatelessWidget {
  const RegimeComparisonView({required this.comparison, super.key});
  final RegimeComparison comparison;

  @override
  Widget build(BuildContext context) {
    final c = comparison;
    final theme = Theme.of(context);
    final String verdict;
    if (c.cheaper == null) {
      verdict = 'Both regimes give the same tax for the year.';
    } else {
      final cheaperRegime = c.cheaper == 'chosen' ? c.chosen : c.other;
      verdict = '${regimeLabel(cheaperRegime)} comes to ${formatBigRupees(paiseText(c.differencePaise))} '
          'less for the year.';
    }
    return Column(
      key: const ValueKey('regime-comparison'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        AmountRow(label: '${regimeLabel(c.chosen)} (in use)', value: formatBigRupees(c.chosenTax)),
        AmountRow(label: regimeLabel(c.other), value: formatBigRupees(c.otherTax)),
        Text(verdict, key: const ValueKey('regime-verdict'), style: theme.textTheme.bodyMedium),
      ],
    );
  }
}

/// TDS on the payslip editor: the computed figure beside the manual one, flags, an override and the
/// way back to the engine. Hidden while the engine is off (no computation, no TDS warning).
class TdsCard extends StatelessWidget {
  const TdsCard({
    required this.payslip,
    super.key,
    this.editable = false,
    this.onUseSuggested,
    this.onHandBack,
  });
  final Payslip payslip;
  final bool editable;

  /// Suggest mode: copy the computed figure into the TDS field.
  final VoidCallback? onUseSuggested;

  /// Apply mode with an override: give the line back to the engine.
  final VoidCallback? onHandBack;

  @override
  Widget build(BuildContext context) {
    final c = payslip.tdsComputation;
    if (c == null) {
      if (!payslip.warnings.contains('TAX_RULES_NOT_VERIFIED')) return const SizedBox.shrink();
      return const SectionCard(
        key: ValueKey('tds-card'),
        title: 'Income tax (TDS)',
        children: [TdsFlagTile('TAX_RULES_NOT_VERIFIED')],
      );
    }
    final theme = Theme.of(context);
    final manual = payslip.inputs?.tds;
    // Warnings of the payslip that belong to the engine, plus the computation's own flags.
    final flags = <String>{
      ...c.flags,
      for (final w in payslip.warnings)
        if (tdsWarningLabels.containsKey(w)) w,
      // Blocking engine errors (RULES_CHANGED, TDS_SHORTFALL_FINAL) arrive in errors[] (19.16).
      for (final e in payslip.errors)
        if (tdsWarningLabels.containsKey(e)) e,
    }.toList();
    final showUse = editable && !c.isApply && manual != c.suggested && onUseSuggested != null;
    return SectionCard(
      key: const ValueKey('tds-card'),
      title: 'Income tax (TDS)',
      children: [
        Text(tdsModeLabel(c.mode), style: theme.textTheme.bodySmall, key: const ValueKey('tds-mode')),
        const SizedBox(height: 4),
        AmountRow(
          key: const ValueKey('tds-suggested'),
          label: 'Computed by the engine',
          value: formatBigRupees(c.suggested),
          bold: true,
          caption: 'For ${c.fy}, ${regimeLabel(c.regime)}',
        ),
        if (c.isApply)
          AmountRow(
            key: const ValueKey('tds-applied'),
            label: 'On the payslip (applied)',
            value: formatBigRupees(c.applied ?? manual),
          )
        else
          AmountRow(
            key: const ValueKey('tds-manual'),
            label: 'On the payslip (typed by HR)',
            value: formatBigRupees(manual),
          ),
        if (c.hasShortfall)
          AmountRow(
            key: const ValueKey('tds-shortfall'),
            label: 'Not deducted this month',
            value: formatBigRupees(c.shortfall),
            caption: 'The pay left was too low. The next months pick it up.',
          ),
        if (c.override != null) ...[
          const SizedBox(height: 4),
          Text(
            'Changed by hand to ${formatBigRupees(c.override!.amount)}. Reason: ${c.override!.reason}',
            key: const ValueKey('tds-override-note'),
            style: theme.textTheme.bodyMedium,
          ),
        ],
        for (final f in flags) TdsFlagTile(f, key: ValueKey('tds-flag-$f')),
        if (showUse || (editable && c.override != null && onHandBack != null))
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                if (showUse)
                  OutlinedButton.icon(
                    key: const ValueKey('tds-use-suggested'),
                    onPressed: onUseSuggested,
                    icon: const Icon(Icons.calculate_outlined),
                    label: const Text('Use the computed figure'),
                  ),
                if (editable && c.override != null && onHandBack != null)
                  OutlinedButton.icon(
                    key: const ValueKey('tds-hand-back'),
                    onPressed: onHandBack,
                    icon: const Icon(Icons.undo),
                    label: const Text('Let the engine set it again'),
                  ),
              ],
            ),
          ),
        Theme(
          data: theme.copyWith(dividerColor: Colors.transparent),
          child: ExpansionTile(
            key: const ValueKey('tds-details'),
            tilePadding: EdgeInsets.zero,
            childrenPadding: EdgeInsets.zero,
            title: const Text('How this was worked out'),
            children: [TdsFigures(computation: c)],
          ),
        ),
      ],
    );
  }
}
