import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../domain/payroll_logic.dart';
import '../domain/payroll_models.dart';
import '../domain/payroll_money.dart';

/// Tamil Nadu half-yearly professional tax slabs (plan 12, section 4.1).
/// VERIFY AGAINST CURRENT TN RULES before the first real run.
const List<PtSlab> tamilNaduDefaultSlabs = [
  PtSlab(minGross: '0.00', maxGross: '21000.00', amount: '0.00', months: [3, 9]),
  PtSlab(minGross: '21001.00', maxGross: '30000.00', amount: '135.00', months: [3, 9]),
  PtSlab(minGross: '30001.00', maxGross: '45000.00', amount: '315.00', months: [3, 9]),
  PtSlab(minGross: '45001.00', maxGross: '60000.00', amount: '690.00', months: [3, 9]),
  PtSlab(minGross: '60001.00', maxGross: '75000.00', amount: '1025.00', months: [3, 9]),
  PtSlab(minGross: '75001.00', amount: '1250.00', months: [3, 9]),
];

class _Row {
  _Row(PtSlab s)
      : min = TextEditingController(text: s.minGross),
        max = TextEditingController(text: s.maxGross ?? ''),
        amount = TextEditingController(text: s.amount),
        months = s.months?.toSet();
  final TextEditingController min;
  final TextEditingController max;
  final TextEditingController amount;

  /// null = every month.
  Set<int>? months;

  void dispose() {
    min.dispose();
    max.dispose();
    amount.dispose();
  }

  PtSlab toSlab() => PtSlab(
        minGross: normalizeMoney(min.text) ?? min.text.trim(),
        maxGross: max.text.trim().isEmpty ? null : (normalizeMoney(max.text) ?? max.text.trim()),
        amount: normalizeMoney(amount.text) ?? amount.text.trim(),
        months: months == null ? null : (months!.toList()..sort()),
      );
}

/// Editor for `payroll.pt_slabs`: add / remove rows, a range and an amount per
/// row, and month chips (or "every month"). Calls [onChanged] with the current
/// slabs after every edit; [PtSlabEditor.error] is shown under the table.
class PtSlabEditor extends StatefulWidget {
  const PtSlabEditor({
    required this.initial,
    required this.onChanged,
    super.key,
    this.enabled = true,
    this.error,
  });
  final List<PtSlab> initial;
  final ValueChanged<List<PtSlab>> onChanged;
  final bool enabled;
  final String? error;

  @override
  State<PtSlabEditor> createState() => _PtSlabEditorState();
}

class _PtSlabEditorState extends State<PtSlabEditor> {
  late final List<_Row> _rows = [for (final s in widget.initial) _Row(s)];

  @override
  void dispose() {
    for (final r in _rows) {
      r.dispose();
    }
    super.dispose();
  }

  void _emit() => widget.onChanged([for (final r in _rows) r.toSlab()]);

  void _replace(List<PtSlab> slabs) {
    setState(() {
      for (final r in _rows) {
        r.dispose();
      }
      _rows
        ..clear()
        ..addAll([for (final s in slabs) _Row(s)]);
    });
    _emit();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final en = widget.enabled;
    final digits = [FilteringTextInputFormatter.allow(RegExp(r'[0-9.]'))];
    const money = TextInputType.numberWithOptions(decimal: true);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (_rows.isEmpty) const Text('No slabs: no professional tax is deducted.'),
        for (var i = 0; i < _rows.length; i++)
          Card(
            key: ValueKey('slab-$i'),
            margin: const EdgeInsets.symmetric(vertical: 4),
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Expanded(child: Text('Slab ${i + 1}', style: theme.textTheme.titleSmall)),
                      if (en)
                        IconButton(
                          tooltip: 'Remove slab ${i + 1}',
                          icon: const Icon(Icons.delete_outline),
                          onPressed: () {
                            setState(() => _rows.removeAt(i).dispose());
                            _emit();
                          },
                        ),
                    ],
                  ),
                  Row(
                    children: [
                      Expanded(
                        child: TextField(
                          controller: _rows[i].min,
                          enabled: en,
                          keyboardType: money,
                          inputFormatters: digits,
                          onChanged: (_) => _emit(),
                          decoration: const InputDecoration(
                            labelText: 'From gross',
                            prefixText: '₹ ',
                            border: OutlineInputBorder(),
                            isDense: true,
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      Expanded(
                        child: TextField(
                          controller: _rows[i].max,
                          enabled: en,
                          keyboardType: money,
                          inputFormatters: digits,
                          onChanged: (_) => _emit(),
                          decoration: const InputDecoration(
                            labelText: 'To gross',
                            hintText: 'No limit',
                            prefixText: '₹ ',
                            border: OutlineInputBorder(),
                            isDense: true,
                          ),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  TextField(
                    controller: _rows[i].amount,
                    enabled: en,
                    keyboardType: money,
                    inputFormatters: digits,
                    onChanged: (_) => _emit(),
                    decoration: const InputDecoration(
                      labelText: 'Tax amount',
                      prefixText: '₹ ',
                      border: OutlineInputBorder(),
                      isDense: true,
                    ),
                  ),
                  SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    dense: true,
                    title: const Text('Every month'),
                    value: _rows[i].months == null,
                    onChanged: en
                        ? (v) {
                            setState(() => _rows[i].months = v ? null : <int>{});
                            _emit();
                          }
                        : null,
                  ),
                  if (_rows[i].months != null)
                    Wrap(
                      spacing: 6,
                      runSpacing: 4,
                      children: [
                        for (var m = 1; m <= 12; m++)
                          FilterChip(
                            key: ValueKey('slab-$i-month-$m'),
                            label: Text(shortMonthNames[m - 1]),
                            selected: _rows[i].months!.contains(m),
                            onSelected: en
                                ? (sel) {
                                    setState(() {
                                      if (sel) {
                                        _rows[i].months!.add(m);
                                      } else {
                                        _rows[i].months!.remove(m);
                                      }
                                    });
                                    _emit();
                                  }
                                : null,
                          ),
                      ],
                    ),
                ],
              ),
            ),
          ),
        if (widget.error != null)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 4),
            child: Text(widget.error!, style: TextStyle(color: theme.colorScheme.error)),
          ),
        if (en)
          Wrap(
            spacing: 8,
            children: [
              TextButton.icon(
                key: const ValueKey('add-slab'),
                onPressed: _rows.length >= 20
                    ? null
                    : () {
                        setState(() => _rows.add(_Row(const PtSlab(minGross: '', amount: '', months: null))));
                        _emit();
                      },
                icon: const Icon(Icons.add),
                label: const Text('Add slab'),
              ),
              TextButton.icon(
                key: const ValueKey('tn-defaults'),
                onPressed: () => _replace(tamilNaduDefaultSlabs),
                icon: const Icon(Icons.restore),
                label: const Text('Tamil Nadu half-yearly defaults'),
              ),
            ],
          ),
      ],
    );
  }
}
