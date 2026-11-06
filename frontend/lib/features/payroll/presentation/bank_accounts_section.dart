import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../admin/presentation/admin_widgets.dart' show adminPermissionsProvider, confirmAction, runWithReauth;
import '../data/payroll_repository.dart';
import '../domain/payroll_models.dart' show canManagePayroll;
import '../domain/phase5a_models.dart';
import 'pay_items_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// Company (debit) bank accounts (contract 19.4). The account number is typed
/// once, sent with the password and never shown again: the list shows the
/// masked number the server returns.
class BankAccountsSection extends ConsumerStatefulWidget {
  const BankAccountsSection({super.key});

  @override
  ConsumerState<BankAccountsSection> createState() => _BankAccountsSectionState();
}

class _BankAccountsSectionState extends ConsumerState<BankAccountsSection> {
  bool _busy = false;

  Future<void> _update(BankAccount a, {bool? isDefault, bool? active}) async {
    if (_busy) return;
    setState(() => _busy = true);
    final repo = ref.read(payrollRepositoryProvider);
    try {
      if (isDefault != null) {
        // Changing the default account is a payment control: it needs the caller's password (19.4).
        final done = await runWithReauth(
          context,
          reason: 'Changing the account that salaries are paid from needs your password.',
          action: (pw) async {
            await repo.updateBankAccount(a.id, isDefault: isDefault, active: active, currentPassword: pw);
          },
        );
        if (!done) return;
      } else {
        await repo.updateBankAccount(a.id, isDefault: isDefault, active: active);
      }
      ref.invalidate(bankAccountsProvider);
    } on ApiException catch (e) {
      if (mounted) payrollSnack(context, phase5ErrorMessage(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _deactivate(BankAccount a) async {
    final ok = await confirmAction(
      context,
      title: 'Deactivate ${a.label}?',
      message: 'It can no longer be chosen for new payment batches. It stays on batches that already paid from it.',
      confirmLabel: 'Deactivate',
    );
    if (ok && mounted) await _update(a, active: false);
  }

  Future<void> _add() async {
    final added = await Navigator.of(context).push<bool>(
      MaterialPageRoute<bool>(builder: (_) => const AddBankAccountScreen()),
    );
    if (added == true) ref.invalidate(bankAccountsProvider);
  }

  @override
  Widget build(BuildContext context) {
    final manage = canManagePayroll(ref.watch(adminPermissionsProvider));
    final async = ref.watch(bankAccountsProvider);
    final theme = Theme.of(context);
    return Scaffold(
      backgroundColor: Colors.transparent,
      floatingActionButton: manage
          ? FloatingActionButton.extended(
              key: const ValueKey('add-bank-account'),
              onPressed: _add,
              icon: const Icon(Icons.add),
              label: const Text('Add account'),
            )
          : null,
      body: payAsync<List<BankAccount>>(
        context,
        async,
        onRetry: () => ref.invalidate(bankAccountsProvider),
        emptyMessage: 'No company bank accounts yet. Add the account that salaries are paid from.',
        isEmpty: (l) => l.isEmpty,
        emptyIcon: Icons.account_balance_outlined,
        loadingLabel: 'Loading bank accounts',
        builder: (accounts) => RefreshIndicator(
          onRefresh: () async => ref.invalidate(bankAccountsProvider),
          child: ListView(
            physics: const AlwaysScrollableScrollPhysics(),
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 96),
            children: [
              for (final a in accounts)
                Card(
                  key: ValueKey('bank-account-${a.id}'),
                  margin: const EdgeInsets.symmetric(vertical: 4),
                  child: Padding(
                    padding: const EdgeInsets.fromLTRB(14, 10, 14, 6),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Row(
                          children: [
                            Expanded(child: Text(a.label, style: theme.textTheme.titleSmall)),
                            if (a.isDefault) const PayStatusChip('Default', tone: PayTone.good),
                            if (!a.active) const PayStatusChip('Inactive'),
                          ],
                        ),
                        Text('${a.bankName}  -  ${a.account}  -  ${a.ifsc}', style: theme.textTheme.bodySmall),
                        if (manage && a.active)
                          Wrap(
                            spacing: 8,
                            children: [
                              if (!a.isDefault)
                                TextButton(
                                  key: ValueKey('bank-default-${a.id}'),
                                  onPressed: _busy ? null : () => _update(a, isDefault: true),
                                  child: const Text('Make default'),
                                ),
                              TextButton(
                                key: ValueKey('bank-deactivate-${a.id}'),
                                onPressed: _busy ? null : () => _deactivate(a),
                                child: const Text('Deactivate'),
                              ),
                            ],
                          ),
                        if (manage && !a.active)
                          TextButton(
                            key: ValueKey('bank-activate-${a.id}'),
                            onPressed: _busy ? null : () => _update(a, active: true),
                            child: const Text('Activate again'),
                          ),
                      ],
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

/// Form for a new company account. Pops `true` when the server created it.
class AddBankAccountScreen extends ConsumerStatefulWidget {
  const AddBankAccountScreen({super.key});

  @override
  ConsumerState<AddBankAccountScreen> createState() => _AddBankAccountScreenState();
}

class _AddBankAccountScreenState extends ConsumerState<AddBankAccountScreen> {
  final _form = GlobalKey<FormState>();
  final _label = TextEditingController();
  final _bank = TextEditingController();
  final _account = TextEditingController();
  final _ifsc = TextEditingController();
  bool _default = false;
  bool _busy = false;
  String? _error;
  final Map<String, String> _serverErrors = {};

  @override
  void dispose() {
    _label.dispose();
    _bank.dispose();
    _account.dispose();
    _ifsc.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    if (_busy) return;
    setState(() {
      _error = null;
      _serverErrors.clear();
    });
    if (!(_form.currentState?.validate() ?? false)) return;
    final input = BankAccountInput(
      label: _label.text,
      bankName: _bank.text,
      account: _account.text,
      ifsc: _ifsc.text,
      isDefault: _default,
    );
    setState(() => _busy = true);
    final repo = ref.read(payrollRepositoryProvider);
    try {
      final done = await runWithReauth(
        context,
        reason: 'Adding a company bank account needs your password.',
        action: (pw) async {
          await repo.createBankAccount(input, currentPassword: pw);
        },
      );
      if (!done || !mounted) return;
      payrollSnack(context, 'Bank account added.');
      Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        for (final d in e.details) {
          if (d.field.isNotEmpty) _serverErrors[d.field] = d.message;
        }
        _error = phase5ErrorMessage(e);
      });
      _form.currentState?.validate();
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Add bank account')),
        body: Form(
          key: _form,
          child: ListView(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 48),
            children: [
              const Text(
                'The account number cannot be changed or shown again after saving: only the last digits are visible. '
                'To correct a mistake, add a new account.',
              ),
              const SizedBox(height: 12),
              TextFormField(
                key: const ValueKey('bank-label'),
                controller: _label,
                enabled: !_busy,
                maxLength: 40,
                decoration: const InputDecoration(labelText: 'Label (for example HDFC salary account)', border: OutlineInputBorder()),
                validator: (v) => validateBankLabel(v) ?? _serverErrors['label'],
              ),
              TextFormField(
                key: const ValueKey('bank-name'),
                controller: _bank,
                enabled: !_busy,
                maxLength: 80,
                decoration: const InputDecoration(labelText: 'Bank name', border: OutlineInputBorder()),
                validator: (v) => validateBankName(v) ?? _serverErrors['bank_name'],
              ),
              TextFormField(
                key: const ValueKey('bank-account-number'),
                controller: _account,
                enabled: !_busy,
                keyboardType: TextInputType.number,
                obscureText: true,
                enableSuggestions: false,
                autocorrect: false,
                maxLength: 18,
                decoration: const InputDecoration(labelText: 'Account number', border: OutlineInputBorder()),
                validator: (v) => validateDebitAccount(v) ?? _serverErrors['account'],
              ),
              TextFormField(
                key: const ValueKey('bank-ifsc'),
                controller: _ifsc,
                enabled: !_busy,
                textCapitalization: TextCapitalization.characters,
                autocorrect: false,
                enableSuggestions: false,
                maxLength: 11,
                decoration: const InputDecoration(labelText: 'IFSC', border: OutlineInputBorder()),
                validator: (v) => validateDebitIfsc(v) ?? _serverErrors['ifsc'],
              ),
              SwitchListTile(
                key: const ValueKey('bank-is-default'),
                contentPadding: EdgeInsets.zero,
                title: const Text('Use as the default debit account'),
                value: _default,
                onChanged: _busy ? null : (v) => setState(() => _default = v),
              ),
              if (_error != null)
                Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: Semantics(
                    liveRegion: true,
                    child: Text(_error!, key: const ValueKey('bank-error'), style: TextStyle(color: theme.colorScheme.error)),
                  ),
                ),
              const SizedBox(height: 16),
              FilledButton(
                key: const ValueKey('bank-save'),
                style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
                onPressed: _busy ? null : _save,
                child: _busy
                    ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                    : const Text('Save account'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
