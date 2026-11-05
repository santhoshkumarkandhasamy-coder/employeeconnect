import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../domain/claims_models.dart';

class _CatRow {
  _CatRow(CategoryDraft c)
      : code = TextEditingController(text: c.code),
        name = TextEditingController(text: c.name),
        max = TextEditingController(text: c.maxAmount ?? ''),
        billRequired = c.billRequired;
  final TextEditingController code;
  final TextEditingController name;
  final TextEditingController max;
  bool billRequired;

  void dispose() {
    code.dispose();
    name.dispose();
    max.dispose();
  }

  CategoryDraft toDraft() => CategoryDraft(
        code: code.text,
        name: name.text,
        maxAmount: max.text.trim().isEmpty ? null : max.text.trim(),
        billRequired: billRequired,
      );
}

/// Editor for `payroll.reimbursement_categories` (up to 20): code, name, an
/// optional limit and whether a bill is needed. Removing a category keeps
/// existing claims; new claims need an active category. Calls [onChanged] with
/// the current rows after every edit.
class CategoriesEditor extends StatefulWidget {
  const CategoriesEditor({
    required this.initial,
    required this.onChanged,
    super.key,
    this.enabled = true,
    this.error,
  });
  final List<CategoryDraft> initial;
  final ValueChanged<List<CategoryDraft>> onChanged;
  final bool enabled;
  final String? error;

  @override
  State<CategoriesEditor> createState() => _CategoriesEditorState();
}

class _CategoriesEditorState extends State<CategoriesEditor> {
  late final List<_CatRow> _rows = [for (final c in widget.initial) _CatRow(c)];

  @override
  void dispose() {
    for (final r in _rows) {
      r.dispose();
    }
    super.dispose();
  }

  void _emit() => widget.onChanged([for (final r in _rows) r.toDraft()]);

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final en = widget.enabled;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (_rows.isEmpty) const Text('No categories: nobody can submit a claim.'),
        for (var i = 0; i < _rows.length; i++)
          Card(
            key: ValueKey('cat-$i'),
            margin: const EdgeInsets.symmetric(vertical: 4),
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Expanded(child: Text('Category ${i + 1}', style: theme.textTheme.titleSmall)),
                      if (en)
                        IconButton(
                          key: ValueKey('cat-remove-$i'),
                          tooltip: 'Remove category ${i + 1}',
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
                          key: ValueKey('cat-code-$i'),
                          controller: _rows[i].code,
                          enabled: en,
                          maxLength: 20,
                          autocorrect: false,
                          inputFormatters: [FilteringTextInputFormatter.allow(RegExp('[a-z0-9_]'))],
                          onChanged: (_) => _emit(),
                          decoration: const InputDecoration(
                            labelText: 'Code',
                            helperText: 'a-z, 0-9, _',
                            counterText: '',
                            border: OutlineInputBorder(),
                            isDense: true,
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      Expanded(
                        child: TextField(
                          key: ValueKey('cat-name-$i'),
                          controller: _rows[i].name,
                          enabled: en,
                          maxLength: 40,
                          onChanged: (_) => _emit(),
                          decoration: const InputDecoration(
                            labelText: 'Name',
                            counterText: '',
                            border: OutlineInputBorder(),
                            isDense: true,
                          ),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  TextField(
                    key: ValueKey('cat-max-$i'),
                    controller: _rows[i].max,
                    enabled: en,
                    keyboardType: const TextInputType.numberWithOptions(decimal: true),
                    inputFormatters: [FilteringTextInputFormatter.allow(RegExp(r'[0-9.]'))],
                    onChanged: (_) => _emit(),
                    decoration: const InputDecoration(
                      labelText: 'Limit per claim',
                      hintText: 'No limit',
                      prefixText: '₹ ',
                      border: OutlineInputBorder(),
                      isDense: true,
                    ),
                  ),
                  SwitchListTile(
                    key: ValueKey('cat-bill-$i'),
                    contentPadding: EdgeInsets.zero,
                    dense: true,
                    title: const Text('Bill required'),
                    value: _rows[i].billRequired,
                    onChanged: en
                        ? (v) {
                            setState(() => _rows[i].billRequired = v);
                            _emit();
                          }
                        : null,
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
          TextButton.icon(
            key: const ValueKey('add-category'),
            onPressed: _rows.length >= 20
                ? null
                : () {
                    setState(() => _rows.add(_CatRow(const CategoryDraft(code: '', name: ''))));
                    _emit();
                  },
            icon: const Icon(Icons.add),
            label: const Text('Add category'),
          ),
      ],
    );
  }
}
