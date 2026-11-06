import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart'
    show AdminPagedBody, adminPermissionsProvider, adminSelfIdProvider, runWithReauth;
import '../data/payroll_repository.dart';
import '../domain/payroll_models.dart' show canManagePayroll;
import '../domain/phase4a_models.dart' show canApprovePayroll;
import '../domain/phase5b_models.dart';
import 'declaration_editor.dart';
import 'pay_items_widgets.dart' show PayTone;
import 'payroll_providers.dart' show payrollClockProvider;
import 'payroll_widgets.dart';
import 'tax_proofs_section.dart';
import 'tax_providers.dart';
import 'tax_widgets.dart';

/// Financial year choices for the admin tax lists: the years that have rules plus this year.
class FyDropdown extends ConsumerWidget {
  const FyDropdown({required this.value, required this.onChanged, super.key, this.fieldKey = 'tax-fy-filter'});
  final String value;
  final ValueChanged<String> onChanged;
  final String fieldKey;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final now = ref.read(payrollClockProvider)();
    final years = ref.watch(taxYearsProvider).valueOrNull ?? const <TaxYear>[];
    final choices = {fyOf(now), value, for (final y in years) y.fy}.toList()..sort((a, b) => b.compareTo(a));
    return DropdownButton<String>(
      key: ValueKey(fieldKey),
      value: value,
      underline: const SizedBox.shrink(),
      items: [for (final c in choices) DropdownMenuItem(value: c, child: Text(fyTitle(c)))],
      onChanged: (v) {
        if (v != null) onChanged(v);
      },
    );
  }
}

class TaxDeclarationsSection extends ConsumerStatefulWidget {
  const TaxDeclarationsSection({super.key});

  @override
  ConsumerState<TaxDeclarationsSection> createState() => _TaxDeclarationsSectionState();
}

class _TaxDeclarationsSectionState extends ConsumerState<TaxDeclarationsSection> {
  late String _fy = fyOf(ref.read(payrollClockProvider)());
  String? _status;
  String _q = '';

  TaxDeclarationQuery get _query => TaxDeclarationQuery(fy: _fy, status: _status, q: _q);

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(taxDeclarationsProvider(_query));
    return AdminPagedBody<DeclarationListItem>(
      async: async,
      emptyMessage: 'No employees match.',
      emptyIcon: Icons.assignment_outlined,
      onRefresh: () async => ref.invalidate(taxDeclarationsProvider(_query)),
      onLoadMore: () => ref.read(taxDeclarationsProvider(_query).notifier).loadMore(),
      header: Padding(
        padding: const EdgeInsets.fromLTRB(12, 0, 12, 4),
        child: Column(
          children: [
            Row(
              children: [
                FyDropdown(value: _fy, onChanged: (v) => setState(() => _fy = v)),
                const SizedBox(width: 12),
                Expanded(
                  child: TextField(
                    key: const ValueKey('decl-search'),
                    decoration: const InputDecoration(
                      hintText: 'Search name or employee ID',
                      prefixIcon: Icon(Icons.search),
                      isDense: true,
                    ),
                    onSubmitted: (v) => setState(() => _q = v.trim()),
                  ),
                ),
              ],
            ),
            SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              child: Row(
                children: [
                  for (final f in <(String?, String)>[
                    (null, 'All'),
                    for (final s in declarationStatusLabels.keys) (s, declarationStatusLabel(s)),
                  ])
                    Padding(
                      padding: const EdgeInsets.only(right: 8),
                      child: ChoiceChip(
                        key: ValueKey('decl-filter-${f.$1 ?? 'all'}'),
                        label: Text(f.$2),
                        selected: _status == f.$1,
                        onSelected: (_) => setState(() => _status = f.$1),
                      ),
                    ),
                ],
              ),
            ),
          ],
        ),
      ),
      itemBuilder: (context, d) => Card(
        key: ValueKey('decl-${d.user.id}'),
        margin: const EdgeInsets.symmetric(vertical: 4),
        child: ListTile(
          title: Text(d.user.display),
          subtitle: Text(
            [
              if (d.regime != null) '${d.regime} regime',
              '${d.items} items',
              if (d.proofs.any) 'proofs: ${d.proofs.submitted} waiting, ${d.proofs.verified} verified',
              if (d.flags.contains('NO_PAN')) 'no PAN on file',
            ].join('  -  '),
          ),
          trailing: TaxStatusChip(
            declarationStatusLabel(d.status),
            tone: d.status == 'submitted' ? (d.dirty ? PayTone.warn : PayTone.good) : PayTone.neutral,
          ),
          onTap: () async {
            await Navigator.of(context).push<void>(
              MaterialPageRoute<void>(builder: (_) => AdminDeclarationScreen(userId: d.user.id, fy: d.fy, name: d.user.display)),
            );
            ref.invalidate(taxDeclarationsProvider(_query));
          },
        ),
      ),
    );
  }
}

class AdminDeclarationScreen extends ConsumerStatefulWidget {
  const AdminDeclarationScreen({required this.userId, required this.fy, required this.name, super.key});
  final String userId;
  final String fy;
  final String name;

  @override
  ConsumerState<AdminDeclarationScreen> createState() => _AdminDeclarationScreenState();
}

class _AdminDeclarationScreenState extends ConsumerState<AdminDeclarationScreen> {
  /// The unmasked copy, held only while this screen shows it.
  TaxDeclaration? _revealed;
  bool _busy = false;

  DeclarationKey get _key => (widget.userId, widget.fy);

  Future<void> _reveal() async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      final repo = ref.read(payrollRepositoryProvider);
      TaxDeclaration? d;
      final done = await runWithReauth(
        context,
        reason: 'Showing full PANs needs your password. The view is recorded.',
        action: (pw) async {
          d = await repo.taxDeclarationOf(widget.userId, fy: widget.fy, reveal: true, currentPassword: pw);
        },
      );
      if (done && mounted) setState(() => _revealed = d);
    } on ApiException catch (e) {
      if (mounted) payrollSnack(context, phase5bErrorMessage(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _unlock() async {
    final canAllowRegime = canApprovePayroll(ref.read(adminPermissionsProvider));
    final r = await showDialog<UnlockChoice>(
      context: context,
      builder: (_) => UnlockDialog(
        fy: widget.fy,
        today: ref.read(payrollClockProvider)(),
        canAllowRegimeChange: canAllowRegime,
      ),
    );
    if (r == null || !mounted) return;
    final repo = ref.read(payrollRepositoryProvider);
    try {
      final done = await runWithReauth(
        context,
        reason: 'Unlocking a declaration needs your password.',
        action: (pw) async {
          await repo.unlockDeclaration(
            widget.userId,
            fy: widget.fy,
            until: r.until,
            reason: r.reason,
            currentPassword: pw,
            allowRegimeChange: canAllowRegime && r.allowRegimeChange,
          );
        },
      );
      if (!done || !mounted) return;
      ref.invalidate(adminDeclarationProvider(_key));
      payrollSnack(context, 'Unlocked until ${r.until}.');
    } on ApiException catch (e) {
      if (mounted) payrollSnack(context, phase5bErrorMessage(e));
    }
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    final selfId = ref.watch(adminSelfIdProvider);
    final manage = canManagePayroll(perms);
    final isSelf = selfId != null && selfId == widget.userId;
    final async = ref.watch(adminDeclarationProvider(_key));
    final sections = ref.watch(verifiedSectionsProvider(widget.fy)).valueOrNull ?? fallbackSections;
    final repo = ref.read(payrollRepositoryProvider);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(
          title: Text(widget.name, maxLines: 1, overflow: TextOverflow.ellipsis),
          actions: [
            IconButton(
              key: const ValueKey('decl-proofs-open'),
              tooltip: 'Proofs',
              icon: const Icon(Icons.fact_check_outlined),
              onPressed: () => Navigator.of(context).push<void>(
                MaterialPageRoute<void>(
                  builder: (_) => TaxProofsScreen(query: TaxProofQuery(fy: widget.fy, status: null, userId: widget.userId)),
                ),
              ),
            ),
            if (manage && !isSelf)
              IconButton(
                key: const ValueKey('decl-unlock'),
                tooltip: 'Unlock',
                icon: const Icon(Icons.lock_open_outlined),
                onPressed: _unlock,
              ),
            if (manage)
              IconButton(
                key: const ValueKey('decl-reveal'),
                tooltip: _revealed == null ? 'Show full PANs' : 'Hide PANs',
                icon: Icon(_revealed == null ? Icons.visibility_outlined : Icons.visibility_off_outlined),
                onPressed: _busy ? null : (_revealed == null ? _reveal : () => setState(() => _revealed = null)),
              ),
          ],
        ),
        body: async.when(
          skipLoadingOnReload: true,
          loading: () => const LoadingView(label: 'Loading declaration'),
          error: (e, _) => ErrorView(message: p5bMsg(e), onRetry: () => ref.invalidate(adminDeclarationProvider(_key))),
          data: (loaded) {
            final d = _revealed ?? loaded;
            return DeclarationPane(
              declaration: d,
              sections: sections,
              canWrite: manage && !isSelf,
              readOnlyNote: !manage
                  ? 'You can view this declaration but not change it.'
                  : (isSelf ? 'You cannot enter your own declaration for yourself on behalf. Use the employee screen.' : null),
              onReload: () => ref.invalidate(adminDeclarationProvider(_key)),
              save: (body) async {
                TaxDeclaration? out;
                final done = await runWithReauth(
                  context,
                  reason: 'Entering a declaration for someone else needs your password.',
                  action: (pw) async {
                    out = await repo.saveDeclarationOnBehalf(widget.userId, body, currentPassword: pw);
                  },
                );
                return done ? out : null;
              },
              submit: (fy, version) async {
                TaxDeclaration? out;
                final done = await runWithReauth(
                  context,
                  reason: 'Submitting a declaration for someone else needs your password.',
                  action: (pw) async {
                    out = await repo.submitDeclarationOnBehalf(
                      widget.userId,
                      fy: fy,
                      expectedVersion: version,
                      currentPassword: pw,
                    );
                  },
                );
                return done ? out : null;
              },
            );
          },
        ),
      ),
    );
  }
}


typedef UnlockChoice = ({String until, String reason, bool allowRegimeChange});

/// Asks until when the employee may edit again, and why (3 to 200 characters). The regime option is only
/// offered to `payroll.approve` holders ([canAllowRegimeChange]); the server enforces it too.
class UnlockDialog extends StatefulWidget {
  const UnlockDialog({required this.fy, required this.today, super.key, this.canAllowRegimeChange = false});
  final String fy;
  final DateTime today;
  final bool canAllowRegimeChange;

  @override
  State<UnlockDialog> createState() => _UnlockDialogState();
}

class _UnlockDialogState extends State<UnlockDialog> {
  late final TextEditingController _until = TextEditingController(text: ymd(widget.today.add(const Duration(days: 7))));
  final _reason = TextEditingController();
  String? _error;
  bool _allowRegime = false;

  @override
  void dispose() {
    _until.dispose();
    _reason.dispose();
    super.dispose();
  }

  void _ok() {
    final d = parseYmd(_until.text.trim());
    final b = fyBounds(widget.fy);
    final t = DateTime(widget.today.year, widget.today.month, widget.today.day);
    String? err;
    if (d == null) {
      err = 'Enter the date as YYYY-MM-DD.';
    } else if (d.isBefore(t)) {
      err = 'The date cannot be in the past.';
    } else if (b != null && d.isAfter(b.$2)) {
      err = 'The date cannot be after the end of ${fyTitle(widget.fy)}.';
    }
    err ??= validateComment(_reason.text, required: true);
    if (err != null) {
      setState(() => _error = err);
      return;
    }
    final UnlockChoice choice = (
      until: ymd(d!),
      reason: _reason.text.trim(),
      allowRegimeChange: widget.canAllowRegimeChange && _allowRegime,
    );
    Navigator.pop(context, choice);
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: const Text('Unlock the declaration'),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text('The employee can change the declaration and add proofs until this date.'),
              const SizedBox(height: 12),
              TextField(
                key: const ValueKey('unlock-until'),
                controller: _until,
                keyboardType: TextInputType.datetime,
                decoration: const InputDecoration(labelText: 'Until (YYYY-MM-DD)', border: OutlineInputBorder()),
              ),
              const SizedBox(height: 10),
              TextField(
                key: const ValueKey('unlock-reason'),
                controller: _reason,
                maxLength: 200,
                decoration: const InputDecoration(labelText: 'Reason', border: OutlineInputBorder()),
              ),
              if (widget.canAllowRegimeChange)
                CheckboxListTile(
                  key: const ValueKey('unlock-allow-regime'),
                  contentPadding: EdgeInsets.zero,
                  controlAffinity: ListTileControlAffinity.leading,
                  value: _allowRegime,
                  onChanged: (v) => setState(() => _allowRegime = v ?? false),
                  title: const Text('Also let the employee change the tax regime'),
                ),
              if (_error != null)
                Text(_error!, key: const ValueKey('unlock-error'), style: TextStyle(color: Theme.of(context).colorScheme.error)),
            ],
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            key: const ValueKey('unlock-confirm'),
            onPressed: _ok,
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: const Text('Unlock'),
          ),
        ],
      );
}
