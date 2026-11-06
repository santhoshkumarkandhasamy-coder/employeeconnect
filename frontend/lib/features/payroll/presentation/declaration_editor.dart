import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../../core/api/api_exception.dart';

import '../domain/payroll_money.dart';
import '../domain/phase5b_models.dart';
import 'payroll_widgets.dart';

/// Investment declaration form shared by the employee screen and HR's "enter on behalf" screen
/// (contract 19.7). The parent owns the server calls (and the password prompt on behalf); this widget owns
/// the local edits. The Save button keeps the draft, Submit freezes it as the submitted version.

class DeclarationEditor extends StatefulWidget {
  const DeclarationEditor({
    required this.declaration,
    required this.sections,
    required this.onSave,
    required this.onSubmit,
    super.key,
    this.canWrite = true,
    this.readOnlyNote,
  });

  final TaxDeclaration declaration;
  final List<SectionDef> sections;

  /// False = show only (for example an admin without `payroll.manage`).
  final bool canWrite;
  final String? readOnlyNote;

  /// Saves the draft; returns the saved declaration, or null when it failed (the parent shows why).
  final Future<TaxDeclaration?> Function(String? regime, List<Map<String, Object?>> items) onSave;

  /// Submits the saved draft; returns the new declaration or null.
  final Future<TaxDeclaration?> Function(TaxDeclaration saved) onSubmit;

  @override
  State<DeclarationEditor> createState() => _DeclarationEditorState();
}

class _DeclarationEditorState extends State<DeclarationEditor> {
  late String? _regime;
  late List<ItemDraft> _items;
  bool _changed = false;
  bool _busy = false;
  String? _error;

  TaxDeclaration get _d => widget.declaration;

  @override
  void initState() {
    super.initState();
    _reset();
  }

  @override
  void didUpdateWidget(DeclarationEditor old) {
    super.didUpdateWidget(old);
    if (old.declaration.version != widget.declaration.version || old.declaration.fy != widget.declaration.fy) {
      _reset();
    }
  }

  void _reset() {
    _regime = _d.draft.regime;
    _items = [for (final i in _d.draft.items) ItemDraft.fromItem(i)];
    _changed = false;
    _error = null;
  }

  SectionDef _def(String code) =>
      widget.sections.where((s) => s.code == code).firstOrNull ?? SectionDef(code: code, name: code);

  bool get _proofOnly => _d.editReason == 'PROOF_WINDOW';
  bool get _editable => widget.canWrite && _d.canEdit && !_busy;

  bool _itemEditable(ItemDraft i) => _editable && (!_proofOnly || _def(i.section).proofRequired);

  List<Map<String, Object?>>? _wire() {
    for (var n = 0; n < _items.length; n++) {
      final i = _items[n];
      final err = validateItemDraft(i, _def(i.section), _d.fy);
      if (err != null) {
        setState(() => _error = '${_def(i.section).code}: $err');
        return null;
      }
    }
    return [for (final i in _items) i.toJson(_def(i.section))];
  }

  Future<TaxDeclaration?> _save() async {
    final wire = _wire();
    if (wire == null) return null;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      return await widget.onSave(_regime, wire);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _submit() async {
    if (_regime == null) {
      setState(() => _error = 'Choose the old or the new regime before you submit.');
      return;
    }
    var saved = _d;
    if (_changed) {
      final s = await _save();
      if (s == null || !mounted) return;
      saved = s;
    }
    if (!mounted) return;
    setState(() => _busy = true);
    try {
      await widget.onSubmit(saved);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _edit(int? index) async {
    final available = [
      for (final s in widget.sections)
        if (!_proofOnly || s.proofRequired) s,
    ];
    final result = await showDialog<ItemDraft>(
      context: context,
      builder: (_) => ItemEditDialog(
        item: index == null ? null : _items[index].copy(),
        sections: available.isEmpty ? widget.sections : available,
        allSections: widget.sections,
        fy: _d.fy,
      ),
    );
    if (result == null || !mounted) return;
    setState(() {
      if (index == null) {
        _items.add(result);
      } else {
        _items[index] = result;
      }
      _changed = true;
      _error = null;
    });
  }

  void _remove(int index) => setState(() {
        _items.removeAt(index);
        _changed = true;
      });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final d = _d;
    final sub = d.submitted;
    final canSubmit = widget.canWrite &&
        d.canEdit &&
        !_busy &&
        (_changed || d.dirty || (sub == null && d.version > 0));
    return ListView(
      padding: const EdgeInsets.fromLTRB(12, 8, 12, 48),
      children: [
        Card(
          child: Padding(
            padding: const EdgeInsets.all(14),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Expanded(child: Text(fyTitle(d.fy), style: theme.textTheme.titleMedium)),
                    if (d.rulesVersion != null) Text('Rules v${d.rulesVersion}', style: theme.textTheme.bodySmall),
                  ],
                ),
                const SizedBox(height: 4),
                Text(
                  d.canEdit ? (editReasonLabels[d.editReason] ?? '') : (editReasonLabels['CLOSED'] ?? ''),
                  key: const ValueKey('decl-edit-reason'),
                ),
                Text('Declarations: ${d.windows.declarationText}', style: theme.textTheme.bodySmall),
                Text('Proofs: ${d.windows.proofText}', style: theme.textTheme.bodySmall),
                if (d.unlockedUntil != null)
                  Text('Unlocked until ${d.unlockedUntil}', style: theme.textTheme.bodySmall),
                if (d.enteredBy != null)
                  Text('Entered by ${d.enteredBy!.display}', key: const ValueKey('decl-entered-by')),
                const Divider(),
                if (sub != null)
                  Row(
                    children: [
                      const Icon(Icons.check_circle_outline, size: 18),
                      const SizedBox(width: 6),
                      Expanded(
                        child: Text(
                          'Submitted version ${sub.version ?? 1}'
                          '${sub.regime == null ? '' : ' (${sub.regime} regime)'}',
                          key: const ValueKey('decl-submitted-badge'),
                        ),
                      ),
                    ],
                  )
                else
                  const Text('Not submitted yet', key: ValueKey('decl-not-submitted')),
                if (sub != null && (d.dirty || _changed))
                  Text(
                    'You have changes that are not submitted yet.',
                    key: const ValueKey('decl-dirty-note'),
                    style: TextStyle(color: theme.colorScheme.tertiary),
                  ),
                if (d.proofs.any)
                  Text(
                    'Proofs: ${d.proofs.submitted} waiting, ${d.proofs.verified} verified, ${d.proofs.rejected} rejected',
                    style: theme.textTheme.bodySmall,
                  ),
              ],
            ),
          ),
        ),
        if (widget.readOnlyNote != null)
          Padding(padding: const EdgeInsets.symmetric(vertical: 6), child: Text(widget.readOnlyNote!)),
        const SizedBox(height: 8),
        Semantics(header: true, child: Text('Tax regime', style: theme.textTheme.titleSmall)),
        const SizedBox(height: 6),
        SegmentedButton<String?>(
          key: const ValueKey('decl-regime'),
          showSelectedIcon: false,
          emptySelectionAllowed: true,
          segments: const [
            ButtonSegment(value: 'new', label: Text('New regime')),
            ButtonSegment(value: 'old', label: Text('Old regime')),
          ],
          selected: {if (_regime != null) _regime},
          onSelectionChanged: _editable && !d.regimeLocked
              ? (s) => setState(() {
                    _regime = s.isEmpty ? null : s.first;
                    _changed = true;
                  })
              : null,
        ),
        if (d.regimeLocked && widget.canWrite)
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Text('The regime is fixed for this year.', style: theme.textTheme.bodySmall),
          ),
        const SizedBox(height: 16),
        Row(
          children: [
            Expanded(child: Semantics(header: true, child: Text('Investments and deductions', style: theme.textTheme.titleSmall))),
            if (_editable && _items.length < maxDeclarationItems)
              FilledButton.tonalIcon(
                key: const ValueKey('decl-add-item'),
                onPressed: () => _edit(null),
                icon: const Icon(Icons.add),
                label: const Text('Add'),
              ),
          ],
        ),
        if (_items.isEmpty)
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 16),
            child: Text('Nothing added yet.', key: ValueKey('decl-empty')),
          ),
        for (var n = 0; n < _items.length; n++) _itemCard(context, n),
        if (_error != null)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Text(_error!, key: const ValueKey('decl-error'), style: TextStyle(color: theme.colorScheme.error)),
          ),
        if (widget.canWrite) ...[
          const SizedBox(height: 16),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              OutlinedButton(
                key: const ValueKey('decl-save'),
                onPressed: _editable && _changed ? _save : null,
                child: const Text('Save draft'),
              ),
              FilledButton(
                key: const ValueKey('decl-submit'),
                onPressed: canSubmit ? _submit : null,
                child: _busy
                    ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                    : const Text('Submit'),
              ),
            ],
          ),
        ],
      ],
    );
  }

  Widget _itemCard(BuildContext context, int n) {
    final i = _items[n];
    final def = _def(i.section);
    final theme = Theme.of(context);
    final editable = _itemEditable(i);
    final amount = def.isHra ? hraAnnualRentPaise(i.details) : null;
    final shown = def.isHra
        ? (amount == null ? '-' : formatRupees(paiseToMoney(amount)))
        : (i.amount.isEmpty ? '-' : formatRupees(i.amount));
    return Card(
      key: ValueKey('decl-item-$n'),
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(14, 8, 6, 8),
        child: Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(def.name, style: theme.textTheme.titleSmall),
                  Text(shown, style: theme.textTheme.titleMedium),
                  if (i.flags.isNotEmpty)
                    Wrap(
                      spacing: 6,
                      children: [
                        for (final f in i.flags)
                          Chip(
                            label: Text(itemFlagLabel(f)),
                            visualDensity: VisualDensity.compact,
                            avatar: const Icon(Icons.info_outline, size: 16),
                          ),
                      ],
                    ),
                ],
              ),
            ),
            if (editable) ...[
              IconButton(
                key: ValueKey('decl-item-edit-$n'),
                tooltip: 'Edit',
                icon: const Icon(Icons.edit_outlined),
                onPressed: () => _edit(n),
              ),
              IconButton(
                key: ValueKey('decl-item-delete-$n'),
                tooltip: 'Remove',
                icon: const Icon(Icons.delete_outline),
                onPressed: () => _remove(n),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

/// Add or edit one item. Fields come from the section's `fields` list. A PAN shown masked by the server
/// must be typed again (the server would otherwise keep the masked text).
class ItemEditDialog extends StatefulWidget {
  const ItemEditDialog({
    required this.sections,
    required this.allSections,
    required this.fy,
    super.key,
    this.item,
  });
  final ItemDraft? item;
  final List<SectionDef> sections;
  final List<SectionDef> allSections;
  final String fy;

  @override
  State<ItemEditDialog> createState() => _ItemEditDialogState();
}

class _ItemEditDialogState extends State<ItemEditDialog> {
  final _form = GlobalKey<FormState>();
  late String _section = widget.item?.section ?? widget.sections.first.code;
  late final TextEditingController _amount = TextEditingController(text: widget.item?.amount ?? '');
  final Map<String, TextEditingController> _text = {};
  final Map<String, bool> _flags = {};
  final Set<String> _maskedOnOpen = {};
  String? _error;

  SectionDef get _def =>
      widget.allSections.where((s) => s.code == _section).firstOrNull ?? SectionDef(code: _section, name: _section);

  @override
  void initState() {
    super.initState();
    final d = widget.item?.details ?? const <String, Object?>{};
    for (final entry in d.entries) {
      final v = entry.value;
      if (v is bool) {
        _flags[entry.key] = v;
      } else if (v != null) {
        final t = '$v';
        if (fieldKindOf(entry.key) == FieldKind.pan && isMaskedPan(t)) {
          _maskedOnOpen.add(entry.key);
          _text[entry.key] = TextEditingController();
        } else {
          _text[entry.key] = TextEditingController(text: t);
        }
      }
    }
  }

  @override
  void dispose() {
    _amount.dispose();
    for (final c in _text.values) {
      c.dispose();
    }
    super.dispose();
  }

  TextEditingController _ctrl(String key) => _text.putIfAbsent(key, TextEditingController.new);

  ItemDraft _build() {
    final d = _def;
    final details = <String, Object?>{};
    for (final f in d.fields) {
      final kind = fieldKindOf(f);
      if (kind == FieldKind.flag) {
        details[f] = _flags[f] ?? false;
      } else {
        final t = _ctrl(f).text.trim();
        if (t.isNotEmpty) details[f] = t;
      }
    }
    return ItemDraft(
      id: widget.item?.id,
      section: _section,
      amount: d.isHra ? '' : _amount.text.trim(),
      details: details,
      flags: widget.item?.flags,
    );
  }

  void _save() {
    if (!(_form.currentState?.validate() ?? false)) return;
    final item = _build();
    final err = validateItemDraft(item, _def, widget.fy);
    if (err != null) {
      setState(() => _error = err);
      return;
    }
    Navigator.pop(context, item);
  }

  Widget _fieldWidget(String f) {
    final kind = fieldKindOf(f);
    if (kind == FieldKind.flag) {
      return SwitchListTile(
        key: ValueKey('item-field-$f'),
        contentPadding: EdgeInsets.zero,
        title: Text(fieldLabel(f)),
        value: _flags[f] ?? false,
        onChanged: (v) => setState(() => _flags[f] = v),
      );
    }
    final needed = _def.isHra && const {'monthly_rent', 'from_month', 'to_month'}.contains(f);
    final hadMasked = _maskedOnOpen.contains(f);
    final landlordNeeded = f == 'landlord_pan' && _def.isHra && _landlordPanNeeded();
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: TextFormField(
        key: ValueKey('item-field-$f'),
        controller: _ctrl(f),
        keyboardType: kind == FieldKind.monthlyMoney || kind == FieldKind.annualMoney
            ? const TextInputType.numberWithOptions(decimal: true)
            : TextInputType.text,
        textCapitalization: kind == FieldKind.pan ? TextCapitalization.characters : TextCapitalization.none,
        inputFormatters: [
          if (kind == FieldKind.monthlyMoney || kind == FieldKind.annualMoney)
            FilteringTextInputFormatter.allow(RegExp(r'[0-9.]')),
          if (kind == FieldKind.pan) LengthLimitingTextInputFormatter(10),
        ],
        onChanged: (_) => setState(() {}),
        decoration: InputDecoration(
          labelText: fieldLabel(f) + (needed || landlordNeeded ? '' : ' (optional)'),
          border: const OutlineInputBorder(),
          helperText: hadMasked ? 'The saved PAN is hidden. Type the full PAN again to keep it.' : null,
        ),
        validator: (v) {
          if (hadMasked && (v ?? '').trim().isEmpty) return 'Type the full PAN again';
          if (kind == FieldKind.pan) return validatePan(v, required: landlordNeeded);
          if (!needed && (v ?? '').trim().isEmpty) return null;
          return validateItemField(f, v, widget.fy);
        },
      ),
    );
  }

  bool _landlordPanNeeded() {
    final threshold = normalizeRulesMoney(_def.landlordPanAbove);
    final rent = hraAnnualRentPaise({
      'monthly_rent': _ctrl('monthly_rent').text.trim(),
      'from_month': _ctrl('from_month').text.trim(),
      'to_month': _ctrl('to_month').text.trim(),
    });
    return threshold != null && rent != null && rent > moneyToPaise(threshold);
  }

  @override
  Widget build(BuildContext context) {
    final d = _def;
    final rentPaise = d.isHra
        ? hraAnnualRentPaise({
            'monthly_rent': _ctrl('monthly_rent').text.trim(),
            'from_month': _ctrl('from_month').text.trim(),
            'to_month': _ctrl('to_month').text.trim(),
          })
        : null;
    return AlertDialog(
      title: Text(widget.item == null ? 'Add an item' : 'Edit item'),
      content: SizedBox(
        width: double.maxFinite,
        child: SingleChildScrollView(
          child: Form(
            key: _form,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (widget.item == null)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 12),
                    child: DropdownButtonFormField<String>(
                      key: const ValueKey('item-section'),
                      initialValue: _section,
                      isExpanded: true,
                      decoration: const InputDecoration(labelText: 'Section', border: OutlineInputBorder()),
                      items: [
                        for (final s in widget.sections)
                          DropdownMenuItem(value: s.code, child: Text(s.name, maxLines: 2, overflow: TextOverflow.ellipsis)),
                      ],
                      onChanged: (v) => setState(() {
                        if (v != null) _section = v;
                      }),
                    ),
                  )
                else
                  Padding(
                    padding: const EdgeInsets.only(bottom: 12),
                    child: Text(d.name, style: Theme.of(context).textTheme.titleSmall),
                  ),
                if (d.limit != null)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 8),
                    child: Text(
                      'Limit ${formatRupees(d.limit)}'
                      '${d.seniorLimit == null ? '' : ' (${formatRupees(d.seniorLimit)} for senior citizens)'}',
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                  ),
                if (!d.isHra)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 10),
                    child: TextFormField(
                      key: const ValueKey('item-amount'),
                      controller: _amount,
                      keyboardType: const TextInputType.numberWithOptions(decimal: true),
                      inputFormatters: [FilteringTextInputFormatter.allow(RegExp(r'[0-9.]'))],
                      decoration: const InputDecoration(
                        labelText: 'Amount for the year',
                        prefixText: '₹ ',
                        border: OutlineInputBorder(),
                      ),
                      validator: validateAnnualAmount,
                    ),
                  ),
                for (final f in d.fields) _fieldWidget(f),
                if (d.isHra)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 8),
                    child: Text(
                      rentPaise == null
                          ? 'The yearly rent is worked out from the monthly rent and the months.'
                          : 'Yearly rent: ${formatRupees(paiseToMoney(rentPaise))}',
                      key: const ValueKey('item-hra-annual'),
                    ),
                  ),
                if (_error != null)
                  Text(_error!, key: const ValueKey('item-error'), style: TextStyle(color: Theme.of(context).colorScheme.error)),
              ],
            ),
          ),
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(
          key: const ValueKey('item-save'),
          onPressed: _save,
          style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
          child: const Text('Done'),
        ),
      ],
    );
  }
}

/// Shows [msg] under the form via the usual snack bar. Convenience for the screens.
void declarationSnack(BuildContext context, String msg) => payrollSnack(context, msg);

/// Holds the current declaration across saves and submits and turns server errors into messages. [save] and
/// [submit] return null when the user cancelled (for example the password prompt) and throw
/// [ApiException] on a server error.
class DeclarationPane extends StatefulWidget {
  const DeclarationPane({
    required this.declaration,
    required this.sections,
    required this.save,
    required this.submit,
    super.key,
    this.canWrite = true,
    this.readOnlyNote,
    this.onReload,
  });
  final TaxDeclaration declaration;
  final List<SectionDef> sections;
  final bool canWrite;
  final String? readOnlyNote;
  final Future<TaxDeclaration?> Function(DeclarationSave body) save;
  final Future<TaxDeclaration?> Function(String fy, int expectedVersion) submit;

  /// Called when the shown declaration is out of date or changed on the server.
  final VoidCallback? onReload;

  @override
  State<DeclarationPane> createState() => _DeclarationPaneState();
}

class _DeclarationPaneState extends State<DeclarationPane> {
  TaxDeclaration? _cur;

  TaxDeclaration get _d => _cur ?? widget.declaration;

  @override
  void didUpdateWidget(DeclarationPane old) {
    super.didUpdateWidget(old);
    if (!identical(old.declaration, widget.declaration)) _cur = null;
  }

  Future<TaxDeclaration?> _run(Future<TaxDeclaration?> Function() call, String done) async {
    try {
      final s = await call();
      if (s == null || !mounted) return null;
      setState(() => _cur = s);
      payrollSnack(context, done);
      widget.onReload?.call();
      return s;
    } on ApiException catch (e) {
      if (isStalePhase5bError(e) || e.code == 'WINDOW_CLOSED' || e.code == 'REGIME_LOCKED') widget.onReload?.call();
      if (mounted) payrollSnack(context, phase5bErrorMessage(e));
      return null;
    }
  }

  @override
  Widget build(BuildContext context) => DeclarationEditor(
        declaration: _d,
        sections: widget.sections,
        canWrite: widget.canWrite,
        readOnlyNote: widget.readOnlyNote,
        onSave: (regime, items) => _run(
          () => widget.save(DeclarationSave(fy: _d.fy, expectedVersion: _d.version, regime: regime, items: items)),
          'Draft saved.',
        ),
        onSubmit: (saved) => _run(
          () => widget.submit(saved.fy, saved.version),
          'Submitted.',
        ),
      );
}
