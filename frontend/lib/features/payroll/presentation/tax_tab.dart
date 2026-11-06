import 'package:flutter/material.dart';

import 'form16_section.dart';
import 'tax_declarations_section.dart';
import 'tax_proofs_section.dart';
import 'tax_rules_section.dart';

/// Tax area of the payroll console (contract 19.6 to 19.8): rules | declarations | proofs | Form 16.
/// Reads need `payroll.view_all`; the actions inside check `payroll.manage` / `payroll.approve`
/// (the server enforces all of it again).
class TaxTab extends StatefulWidget {
  const TaxTab({super.key});

  @override
  State<TaxTab> createState() => _TaxTabState();
}

enum _Part { rules, declarations, proofs, form16 }

class _TaxTabState extends State<TaxTab> {
  _Part _part = _Part.rules;

  @override
  Widget build(BuildContext context) => Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
            child: SegmentedButton<_Part>(
              key: const ValueKey('tax-parts'),
              showSelectedIcon: false,
              segments: const [
                ButtonSegment(value: _Part.rules, label: Text('Rules')),
                ButtonSegment(value: _Part.declarations, label: Text('Declared')),
                ButtonSegment(value: _Part.proofs, label: Text('Proofs')),
                ButtonSegment(value: _Part.form16, label: Text('Form 16')),
              ],
              selected: {_part},
              onSelectionChanged: (s) => setState(() => _part = s.first),
            ),
          ),
          Expanded(
            child: switch (_part) {
              _Part.rules => const TaxRulesSection(),
              _Part.declarations => const TaxDeclarationsSection(),
              _Part.proofs => const TaxProofsSection(),
              _Part.form16 => const Form16Section(),
            },
          ),
        ],
      );
}
