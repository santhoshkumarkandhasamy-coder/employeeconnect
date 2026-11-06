import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../data/payroll_repository.dart';
import '../domain/phase5b_models.dart';
import 'payroll_widgets.dart';

/// Editor of a DRAFT tax rules version (contract 19.6). Slabs, standard deduction, rebate, surcharge,
/// allowed sections, the section catalogue, HRA percentages and group limits are edited here; every other
/// value of the document (labels, rounding, component treatment, ...) is kept exactly as loaded. The server
/// validates the whole document again and names the failing JSON path.
class TaxRulesEditorScreen extends ConsumerStatefulWidget {
  const TaxRulesEditorScreen({required this.rules, super.key});
  final TaxRules rules;

  @override
  ConsumerState<TaxRulesEditorScreen> createState() => _TaxRulesEditorScreenState();
}

class _TaxRulesEditorScreenState extends ConsumerState<TaxRulesEditorScreen> {
  late Map<String, dynamic> _rules;
  late final TextEditingController _note = TextEditingController(text: widget.rules.note ?? '');
  late int _versionRow = widget.rules.versionRow;
  List<RulesIssue> _issues = const [];
  String? _serverError;
  bool _saving = false;

  @override
  void initState() {
    super.initState();
    _rules = deepCopyMap(widget.rules.rules)..remove('note');
    _rules['sections'] ??= <dynamic>[];
    for (final r in _regimes.values) {
      r['surcharge'] ??= <dynamic>[];
      _relink(_slabs(r));
    }
  }

  @override
  void dispose() {
    _note.dispose();
    super.dispose();
  }

  Map<String, Map<String, dynamic>> get _regimes {
    final r = _rules['regimes'];
    return r is Map ? {for (final e in r.entries) '${e.key}': (e.value as Map).cast<String, dynamic>()} : {};
  }

  List<Map<String, dynamic>> _slabs(Map<String, dynamic> regime) =>
      (regime['slabs'] as List).cast<Map<String, dynamic>>();

  List<Map<String, dynamic>> get _sections => ((_rules['sections'] as List?) ?? const []).cast<Map<String, dynamic>>();

  /// First slab starts at 0.00, each next one where the one before ends, the last one is open-ended.
  void _relink(List<Map<String, dynamic>> slabs) {
    for (var i = 0; i < slabs.length; i++) {
      slabs[i]['from'] = i == 0 ? '0.00' : '${slabs[i - 1]['to'] ?? ''}';
    }
    if (slabs.isNotEmpty) slabs.last['to'] = null;
  }

  void _touch() => setState(() {
        _issues = const [];
        _serverError = null;
      });

  /// Money typed as 400000 becomes 400000.00; invalid text stays as typed so validation names it.
  void _normalize() {
    String? n(Object? v) => v is String ? (normalizeRulesMoney(v) ?? v) : null;
    for (final r in _regimes.values) {
      for (final s in _slabs(r)) {
        s['from'] = n(s['from']) ?? s['from'];
        if (s['to'] != null) s['to'] = n(s['to']) ?? s['to'];
      }
      r['standard_deduction'] = n(r['standard_deduction']) ?? r['standard_deduction'];
      final rb = (r['rebate'] as Map).cast<String, dynamic>();
      rb['income_limit'] = n(rb['income_limit']) ?? rb['income_limit'];
      rb['max'] = n(rb['max']) ?? rb['max'];
      for (final s in ((r['surcharge'] as List?) ?? const []).cast<Map<String, dynamic>>()) {
        s['above'] = n(s['above']) ?? s['above'];
      }
    }
    for (final s in _sections) {
      for (final k in ['limit', 'senior_limit', 'landlord_pan_above']) {
        if (s[k] is String) s[k] = n(s[k]);
      }
    }
    for (final g in ((_rules['group_limits'] as List?) ?? const []).cast<Map<String, dynamic>>()) {
      g['limit'] = n(g['limit']) ?? g['limit'];
    }
  }

  Future<void> _save() async {
    if (_saving) return;
    _normalize();
    final issues = validateRulesLocally(_rules);
    if (issues.isNotEmpty) {
      setState(() {
        _issues = issues;
        _serverError = null;
      });
      return;
    }
    setState(() {
      _saving = true;
      _issues = const [];
      _serverError = null;
    });
    try {
      final saved = await ref.read(payrollRepositoryProvider).saveTaxRules(
            widget.rules.id,
            versionRow: _versionRow,
            rules: deepCopyMap(_rules),
            note: _note.text,
          );
      _versionRow = saved.versionRow;
      if (!mounted) return;
      payrollSnack(context, 'Saved. Submit the draft when it is ready.');
      Navigator.pop(context);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _serverError = phase5bErrorMessage(e));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(
          title: Text('Edit ${fyTitle(widget.rules.fy)} v${widget.rules.version}'),
          actions: [
            TextButton(
              key: const ValueKey('rules-editor-save'),
              onPressed: _saving ? null : _save,
              child: _saving
                  ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Text('Save'),
            ),
          ],
        ),
        body: Column(
          children: [
            if (_issues.isNotEmpty || _serverError != null)
              ConstrainedBox(
                constraints: const BoxConstraints(maxHeight: 220),
                child: SingleChildScrollView(
                  padding: const EdgeInsets.fromLTRB(12, 8, 12, 0),
                  child: Card(
                    color: theme.colorScheme.errorContainer,
                    margin: EdgeInsets.zero,
                    child: Padding(
                      padding: const EdgeInsets.all(12),
                      child: Column(
                        key: const ValueKey('rules-editor-issues'),
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text('Fix these before saving', style: theme.textTheme.titleSmall),
                          if (_serverError != null) Text(_serverError!),
                          for (final i in _issues.take(12)) Text('${i.path}: ${i.message}'),
                          if (_issues.length > 12) Text('... and ${_issues.length - 12} more'),
                        ],
                      ),
                    ),
                  ),
                ),
              ),
            Expanded(child: _form()),
          ],
        ),
      ),
    );
  }

  Widget _form() {
    return ListView(
          padding: const EdgeInsets.fromLTRB(12, 8, 12, 64),
          children: [
            TextFormField(
              key: const ValueKey('rules-note-field'),
              controller: _note,
              maxLength: 300,
              maxLines: 3,
              minLines: 1,
              onChanged: (_) => _touch(),
              decoration: const InputDecoration(
                labelText: 'Note (say what these values are based on)',
                helperText: 'A note that starts with UNVERIFIED blocks submission.',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 12),
            _general(),
            for (final e in _regimes.entries) _regimeCard(e.key, e.value),
            _sectionsCard(),
            _hraCard(),
            _groupLimitsCard(),
            const Padding(
              padding: EdgeInsets.only(top: 12),
              child: Text(
                'Everything else in the document (labels, rounding, HRA cities, component treatment) is kept as it is.',
              ),
            ),
          ],
    );
  }

  Widget _general() => SectionCard(
        title: 'General',
        children: [
          DropdownButtonFormField<String>(
            key: const ValueKey('rules-default-regime'),
            initialValue: _regimes.containsKey(_rules['default_regime']) ? '${_rules['default_regime']}' : null,
            decoration: const InputDecoration(labelText: 'Regime when nothing is chosen', border: OutlineInputBorder()),
            items: [for (final k in _regimes.keys) DropdownMenuItem(value: k, child: Text(k))],
            onChanged: (v) {
              _rules['default_regime'] = v;
              _touch();
            },
          ),
          const SizedBox(height: 10),
          _RField(
            fieldKey: 'rules-cess',
            label: 'Cess (%)',
            initial: '${_rules['cess_pct'] ?? ''}',
            rate: true,
            onChanged: (v) {
              _rules['cess_pct'] = v;
              _touch();
            },
          ),
        ],
      );

  Widget _regimeCard(String name, Map<String, dynamic> r) {
    final slabs = _slabs(r);
    final rebate = (r['rebate'] as Map).cast<String, dynamic>();
    final surcharge = ((r['surcharge'] as List?) ?? const []).cast<Map<String, dynamic>>();
    final codes = [for (final s in _sections) '${s['code']}'];
    final allowed = ((r['allowed_sections'] as List?) ?? const []).map((e) => '$e').toList();
    return SectionCard(
      title: '${name[0].toUpperCase()}${name.substring(1)} regime',
      children: [
        Text('Tax slabs', style: Theme.of(context).textTheme.titleSmall),
        for (var i = 0; i < slabs.length; i++)
          Padding(
            key: ObjectKey(slabs[i]),
            padding: const EdgeInsets.symmetric(vertical: 4),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Expanded(
                  flex: 3,
                  child: Padding(
                    padding: const EdgeInsets.only(top: 16),
                    child: Text('From ${slabs[i]['from']}', key: ValueKey('$name-slab-$i-from')),
                  ),
                ),
                Expanded(
                  flex: 3,
                  child: i == slabs.length - 1
                      ? const Padding(
                          padding: EdgeInsets.only(top: 16),
                          child: Text('No upper limit'),
                        )
                      : _RField(
                          fieldKey: '$name-slab-$i-to',
                          label: 'To',
                          initial: '${slabs[i]['to'] ?? ''}',
                          money: true,
                          onChanged: (v) {
                            slabs[i]['to'] = v;
                            _relink(slabs);
                            _touch();
                          },
                        ),
                ),
                const SizedBox(width: 8),
                Expanded(
                  flex: 2,
                  child: _RField(
                    fieldKey: '$name-slab-$i-rate',
                    label: 'Rate %',
                    initial: '${slabs[i]['rate_pct'] ?? ''}',
                    rate: true,
                    onChanged: (v) {
                      slabs[i]['rate_pct'] = v;
                      _touch();
                    },
                  ),
                ),
                IconButton(
                  key: ValueKey('$name-slab-$i-remove'),
                  tooltip: 'Remove slab',
                  icon: const Icon(Icons.delete_outline),
                  onPressed: slabs.length > 1
                      ? () {
                          slabs.removeAt(i);
                          _relink(slabs);
                          _touch();
                        }
                      : null,
                ),
              ],
            ),
          ),
        Align(
          alignment: Alignment.centerLeft,
          child: TextButton.icon(
            key: ValueKey('$name-slab-add'),
            onPressed: () {
              if (slabs.isNotEmpty) slabs.last['to'] = '';
              slabs.add({'from': '', 'to': null, 'rate_pct': '0'});
              _relink(slabs);
              _touch();
            },
            icon: const Icon(Icons.add),
            label: const Text('Add slab'),
          ),
        ),
        const Divider(),
        _RField(
          fieldKey: '$name-std',
          label: 'Standard deduction',
          initial: '${r['standard_deduction'] ?? ''}',
          money: true,
          onChanged: (v) {
            r['standard_deduction'] = v;
            _touch();
          },
        ),
        const SizedBox(height: 10),
        _RField(
          fieldKey: '$name-rebate-limit',
          label: 'Rebate: income up to',
          initial: '${rebate['income_limit'] ?? ''}',
          money: true,
          onChanged: (v) {
            rebate['income_limit'] = v;
            _touch();
          },
        ),
        const SizedBox(height: 10),
        _RField(
          fieldKey: '$name-rebate-max',
          label: 'Rebate: at most',
          initial: '${rebate['max'] ?? ''}',
          money: true,
          onChanged: (v) {
            rebate['max'] = v;
            _touch();
          },
        ),
        SwitchListTile(
          key: ValueKey('$name-rebate-relief'),
          contentPadding: EdgeInsets.zero,
          title: const Text('Marginal relief on the rebate'),
          value: rebate['marginal_relief'] == true,
          onChanged: (v) {
            rebate['marginal_relief'] = v;
            _touch();
          },
        ),
        const Divider(),
        Text('Surcharge', style: Theme.of(context).textTheme.titleSmall),
        for (var i = 0; i < surcharge.length; i++)
          Padding(
            key: ObjectKey(surcharge[i]),
            padding: const EdgeInsets.symmetric(vertical: 4),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Expanded(
                  flex: 3,
                  child: _RField(
                    fieldKey: '$name-sur-$i-above',
                    label: 'Income above',
                    initial: '${surcharge[i]['above'] ?? ''}',
                    money: true,
                    onChanged: (v) {
                      surcharge[i]['above'] = v;
                      _touch();
                    },
                  ),
                ),
                const SizedBox(width: 8),
                Expanded(
                  flex: 2,
                  child: _RField(
                    fieldKey: '$name-sur-$i-rate',
                    label: 'Rate %',
                    initial: '${surcharge[i]['rate_pct'] ?? ''}',
                    rate: true,
                    onChanged: (v) {
                      surcharge[i]['rate_pct'] = v;
                      _touch();
                    },
                  ),
                ),
                IconButton(
                  key: ValueKey('$name-sur-$i-remove'),
                  tooltip: 'Remove row',
                  icon: const Icon(Icons.delete_outline),
                  onPressed: () {
                    surcharge.removeAt(i);
                    _touch();
                  },
                ),
              ],
            ),
          ),
        Align(
          alignment: Alignment.centerLeft,
          child: TextButton.icon(
            key: ValueKey('$name-sur-add'),
            onPressed: () {
              surcharge.add({'above': '', 'rate_pct': '0'});
              r['surcharge'] = surcharge;
              _touch();
            },
            icon: const Icon(Icons.add),
            label: const Text('Add row'),
          ),
        ),
        SwitchListTile(
          key: ValueKey('$name-sur-relief'),
          contentPadding: EdgeInsets.zero,
          title: const Text('Marginal relief on the surcharge'),
          value: r['surcharge_marginal_relief'] == true,
          onChanged: (v) {
            r['surcharge_marginal_relief'] = v;
            _touch();
          },
        ),
        SwitchListTile(
          key: ValueKey('$name-pt'),
          contentPadding: EdgeInsets.zero,
          title: const Text('Professional tax is deductible'),
          value: r['pt_deductible'] == true,
          onChanged: (v) {
            r['pt_deductible'] = v;
            _touch();
          },
        ),
        SwitchListTile(
          key: ValueKey('$name-hra'),
          contentPadding: EdgeInsets.zero,
          title: const Text('HRA exemption applies'),
          value: r['hra_exemption'] == true,
          onChanged: (v) {
            r['hra_exemption'] = v;
            _touch();
          },
        ),
        const Divider(),
        Text('Sections allowed in this regime', style: Theme.of(context).textTheme.titleSmall),
        // Codes that the list names but the catalogue does not define stay visible so they can be removed.
        Wrap(
          spacing: 6,
          children: [
            for (final c in {...codes, ...allowed})
              FilterChip(
                key: ValueKey('$name-allowed-$c'),
                label: Text(c),
                selected: allowed.contains(c),
                avatar: codes.contains(c) ? null : const Icon(Icons.warning_amber_outlined, size: 16),
                onSelected: (on) {
                  final list = allowed.toList();
                  on ? list.add(c) : list.remove(c);
                  r['allowed_sections'] = list;
                  _touch();
                },
              ),
          ],
        ),
      ],
    );
  }

  static const _fieldChoices = [
    'description',
    'senior',
    'lender_name',
    'lender_pan',
    'monthly_rent',
    'from_month',
    'to_month',
    'landlord_name',
    'landlord_pan',
    'city',
    'metro',
    'income',
    'tds',
    'pt',
    'pf',
  ];

  Widget _sectionsCard() {
    final secs = _sections;
    return SectionCard(
      title: 'Sections people can declare',
      children: [
        for (var i = 0; i < secs.length; i++)
          ExpansionTile(
            key: ObjectKey(secs[i]),
            tilePadding: EdgeInsets.zero,
            title: Text('${secs[i]['code']}', key: ValueKey('section-$i-title')),
            subtitle: Text('${secs[i]['name'] ?? ''}', maxLines: 1, overflow: TextOverflow.ellipsis),
            children: [
              _RField(
                fieldKey: 'section-$i-code',
                label: 'Code (capitals, digits, _)',
                initial: '${secs[i]['code'] ?? ''}',
                onChanged: (v) {
                  secs[i]['code'] = v.toUpperCase();
                  _touch();
                },
              ),
              const SizedBox(height: 8),
              _RField(
                fieldKey: 'section-$i-name',
                label: 'Name',
                initial: '${secs[i]['name'] ?? ''}',
                onChanged: (v) {
                  secs[i]['name'] = v;
                  _touch();
                },
              ),
              const SizedBox(height: 8),
              _RField(
                fieldKey: 'section-$i-limit',
                label: 'Limit (empty = none)',
                initial: '${secs[i]['limit'] ?? ''}',
                money: true,
                optional: true,
                onChanged: (v) {
                  secs[i]['limit'] = v.trim().isEmpty ? null : v;
                  _touch();
                },
              ),
              const SizedBox(height: 8),
              _RField(
                fieldKey: 'section-$i-senior',
                label: 'Limit for senior citizens (empty = none)',
                initial: '${secs[i]['senior_limit'] ?? ''}',
                money: true,
                optional: true,
                onChanged: (v) {
                  if (v.trim().isEmpty) {
                    secs[i].remove('senior_limit');
                  } else {
                    secs[i]['senior_limit'] = v;
                  }
                  _touch();
                },
              ),
              const SizedBox(height: 8),
              _RField(
                fieldKey: 'section-$i-group',
                label: 'Group (sections in a group share a limit)',
                initial: '${secs[i]['group'] ?? ''}',
                optional: true,
                onChanged: (v) {
                  secs[i]['group'] = v.trim().isEmpty ? null : v.trim();
                  _touch();
                },
              ),
              SwitchListTile(
                key: ValueKey('section-$i-proof'),
                contentPadding: EdgeInsets.zero,
                title: const Text('A proof is needed'),
                value: secs[i]['proof_required'] != false,
                onChanged: (v) {
                  secs[i]['proof_required'] = v;
                  _touch();
                },
              ),
              Wrap(
                spacing: 6,
                children: [
                  for (final f in _fieldChoices)
                    FilterChip(
                      key: ValueKey('section-$i-field-$f'),
                      label: Text(fieldLabel(f)),
                      selected: ((secs[i]['fields'] as List?) ?? const []).contains(f),
                      onSelected: (on) {
                        final list = ((secs[i]['fields'] as List?) ?? const []).map((e) => '$e').toList();
                        on ? list.add(f) : list.remove(f);
                        secs[i]['fields'] = list;
                        _touch();
                      },
                    ),
                ],
              ),
              Align(
                alignment: Alignment.centerRight,
                child: TextButton.icon(
                  key: ValueKey('section-$i-remove'),
                  onPressed: () {
                    secs.removeAt(i);
                    _touch();
                  },
                  icon: const Icon(Icons.delete_outline),
                  label: const Text('Remove section'),
                ),
              ),
            ],
          ),
        Align(
          alignment: Alignment.centerLeft,
          child: TextButton.icon(
            key: const ValueKey('section-add'),
            onPressed: secs.length >= 40
                ? null
                : () {
                    secs.add({
                      'code': '',
                      'name': '',
                      'limit': null,
                      'group': null,
                      'proof_required': true,
                      'fields': ['description'],
                    });
                    _rules['sections'] = secs;
                    _touch();
                  },
            icon: const Icon(Icons.add),
            label: const Text('Add section'),
          ),
        ),
      ],
    );
  }

  Widget _hraCard() {
    final hra = _rules['hra'];
    if (hra is! Map) return const SizedBox.shrink();
    final h = hra.cast<String, dynamic>();
    Widget f(String key, String label) => Padding(
          padding: const EdgeInsets.only(bottom: 10),
          child: _RField(
            fieldKey: 'hra-$key',
            label: label,
            initial: '${h[key] ?? ''}',
            rate: true,
            onChanged: (v) {
              h[key] = v;
              _touch();
            },
          ),
        );
    return SectionCard(
      title: 'House rent allowance',
      children: [
        f('metro_pct', 'Metro cities (% of basic)'),
        f('non_metro_pct', 'Other cities (% of basic)'),
        f('rent_minus_basic_pct', 'Rent above (% of basic)'),
      ],
    );
  }

  Widget _groupLimitsCard() {
    final groups = ((_rules['group_limits'] as List?) ?? const []).cast<Map<String, dynamic>>();
    if (groups.isEmpty) return const SizedBox.shrink();
    return SectionCard(
      title: 'Shared limits',
      children: [
        for (var i = 0; i < groups.length; i++)
          Padding(
            padding: const EdgeInsets.only(bottom: 10),
            child: _RField(
              fieldKey: 'group-$i-limit',
              label: 'Limit of group ${groups[i]['group']}',
              initial: '${groups[i]['limit'] ?? ''}',
              money: true,
              onChanged: (v) {
                groups[i]['limit'] = v;
                _touch();
              },
            ),
          ),
      ],
    );
  }
}

/// A text field that writes into the working copy on every change.
class _RField extends StatelessWidget {
  const _RField({
    required this.fieldKey,
    required this.label,
    required this.initial,
    required this.onChanged,
    this.money = false,
    this.rate = false,
    this.optional = false,
  });
  final String fieldKey;
  final String label;
  final String initial;
  final bool money;
  final bool rate;
  final bool optional;
  final ValueChanged<String> onChanged;

  @override
  Widget build(BuildContext context) => TextFormField(
        key: ValueKey(fieldKey),
        initialValue: initial,
        keyboardType: money || rate ? const TextInputType.numberWithOptions(decimal: true) : TextInputType.text,
        inputFormatters: [if (money || rate) FilteringTextInputFormatter.allow(RegExp(r'[0-9.]'))],
        onChanged: onChanged,
        decoration: InputDecoration(
          labelText: label,
          border: const OutlineInputBorder(),
          isDense: true,
        ),
      );
}
