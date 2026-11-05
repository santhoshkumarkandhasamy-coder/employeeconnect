import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart'
    show AdminPagedBody, NoPermissionScreen, adminPermissionsProvider, confirmAction;
import '../data/claim_bill_picker.dart';
import '../data/payroll_repository.dart';
import '../domain/claims_models.dart';
import '../domain/payroll_models.dart';
import '../domain/payroll_money.dart';
import 'claims_widgets.dart';
import 'payroll_providers.dart';
import 'payroll_widgets.dart';

/// "My claims" (expense reimbursements), opened from the Payslips screen. Needs
/// `reimbursement.submit`. List with status filter, "New claim", and a detail
/// with a Cancel button while the claim is submitted or verified.
/// Screenshot-protected; in memory only.
class MyClaimsScreen extends ConsumerStatefulWidget {
  const MyClaimsScreen({super.key});

  @override
  ConsumerState<MyClaimsScreen> createState() => _MyClaimsScreenState();
}

class _MyClaimsScreenState extends ConsumerState<MyClaimsScreen> {
  String? _status;

  ClaimQuery get _query => ClaimQuery(status: _status);

  Future<void> _newClaim() async {
    final done = await Navigator.of(context).push<bool>(
      MaterialPageRoute<bool>(builder: (_) => const ClaimFormScreen()),
    );
    if (done == true) ref.invalidate(myClaimsProvider);
  }

  Future<void> _open(Claim c) async {
    await Navigator.of(context).push<void>(
      MaterialPageRoute<void>(builder: (_) => ClaimDetailScreen(claimId: c.id)),
    );
    ref.invalidate(myClaimsProvider);
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!canSubmitClaims(perms)) return const NoPermissionScreen(title: 'My claims');
    final q = _query;
    final async = ref.watch(myClaimsProvider(q));
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('My claims')),
        floatingActionButton: FloatingActionButton.extended(
          key: const ValueKey('new-claim'),
          onPressed: _newClaim,
          icon: const Icon(Icons.add),
          label: const Text('New claim'),
        ),
        body: AdminPagedBody<Claim>(
          async: async,
          emptyMessage: _status == null
              ? 'You have no expense claims yet.'
              : 'No ${claimStatusLabel(_status!).toLowerCase()} claims.',
          emptyIcon: Icons.receipt_outlined,
          onRefresh: () async => ref.read(myClaimsProvider(q).notifier).reload(),
          onLoadMore: () => ref.read(myClaimsProvider(q).notifier).loadMore(),
          header: SizedBox(
            height: 52,
            child: ListView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
              children: [
                Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: ChoiceChip(
                    key: const ValueKey('claims-filter-all'),
                    label: const Text('All'),
                    selected: _status == null,
                    onSelected: (_) => setState(() => _status = null),
                  ),
                ),
                for (final s in claimFilterStatuses)
                  Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: ChoiceChip(
                      key: ValueKey('claims-filter-$s'),
                      label: Text(claimStatusLabel(s)),
                      selected: _status == s,
                      onSelected: (_) => setState(() => _status = s),
                    ),
                  ),
              ],
            ),
          ),
          itemBuilder: (context, c) => ClaimTile(claim: c, onTap: () => _open(c)),
        ),
      ),
    );
  }
}

/// One own claim: amounts, payout month, the decision comment, the bill (fetched
/// with the bearer header) and Cancel while submitted / verified.
class ClaimDetailScreen extends ConsumerStatefulWidget {
  const ClaimDetailScreen({required this.claimId, super.key});
  final String claimId;

  @override
  ConsumerState<ClaimDetailScreen> createState() => _ClaimDetailScreenState();
}

class _ClaimDetailScreenState extends ConsumerState<ClaimDetailScreen> {
  bool _busy = false;

  Future<void> _cancel(Claim c) async {
    final ok = await confirmAction(
      context,
      title: 'Cancel this claim?',
      message: 'The claim for ${formatRupees(c.amount)} will be withdrawn. This cannot be undone.',
      confirmLabel: 'Cancel claim',
    );
    if (!ok || !mounted) return;
    final repo = ref.read(payrollRepositoryProvider);
    setState(() => _busy = true);
    try {
      await repo.cancelClaim(c.id);
      if (mounted) payrollSnack(context, 'Claim cancelled.');
    } on ApiException catch (e) {
      if (mounted) payrollSnack(context, payrollErrorMessage(e));
    } finally {
      // NOT_PENDING and the like mean the state changed: reload either way.
      ref.invalidate(myClaimProvider(widget.claimId));
      ref.invalidate(myClaimsProvider);
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(myClaimProvider(widget.claimId));
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Claim')),
        body: async.when(
          skipLoadingOnReload: true,
          loading: () => const LoadingView(label: 'Loading claim'),
          error: (e, _) => ErrorView(
            message: payrollMsg(e),
            onRetry: () => ref.invalidate(myClaimProvider(widget.claimId)),
          ),
          data: (c) => ListView(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
            children: [
              ClaimDetailBody(claim: c),
              if (c.canCancel)
                Padding(
                  padding: const EdgeInsets.only(top: 12),
                  child: OutlinedButton.icon(
                    key: const ValueKey('cancel-claim'),
                    onPressed: _busy ? null : () => _cancel(c),
                    icon: const Icon(Icons.close),
                    label: const Text('Cancel claim'),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

/// New claim: category (with its cap and whether a bill is needed), expense
/// date, amount, description and the bill. The bill is uploaded first
/// (`POST /files`, purpose `reimbursement_bill`), then the claim is created
/// with its file id.
class ClaimFormScreen extends ConsumerStatefulWidget {
  const ClaimFormScreen({super.key});

  @override
  ConsumerState<ClaimFormScreen> createState() => _ClaimFormScreenState();
}

class _ClaimFormScreenState extends ConsumerState<ClaimFormScreen> {
  final _form = GlobalKey<FormState>();
  final _amount = TextEditingController();
  final _desc = TextEditingController();
  ClaimCategory? _category;
  DateTime? _date;
  PickedBill? _bill;
  String? _billFileId; // kept after a successful upload so a retry does not upload twice
  String? _billError;
  String? _error;
  bool _busy = false;
  bool _uploading = false;
  bool _submitted = false;

  @override
  void dispose() {
    _amount.dispose();
    _desc.dispose();
    super.dispose();
  }

  Future<void> _pickBill() async {
    final src = await showModalBottomSheet<BillSource>(
      context: context,
      showDragHandle: true,
      builder: (ctx) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              key: const ValueKey('bill-camera'),
              leading: const Icon(Icons.photo_camera_outlined),
              title: const Text('Take a photo'),
              onTap: () => Navigator.pop(ctx, BillSource.camera),
            ),
            ListTile(
              key: const ValueKey('bill-gallery'),
              leading: const Icon(Icons.photo_library_outlined),
              title: const Text('Choose an image'),
              subtitle: const Text('JPEG or PNG, up to 10 MB'),
              onTap: () => Navigator.pop(ctx, BillSource.gallery),
            ),
            ListTile(
              key: const ValueKey('bill-document'),
              leading: const Icon(Icons.picture_as_pdf_outlined),
              title: const Text('Choose a PDF or file'),
              subtitle: const Text('PDF, JPEG or PNG, up to 10 MB'),
              onTap: () => Navigator.pop(ctx, BillSource.document),
            ),
          ],
        ),
      ),
    );
    if (src == null || !mounted) return;
    final picked = await ref.read(claimBillPickerProvider).pick(src);
    if (picked == null || !mounted) return;
    setState(() {
      if (picked.ok) {
        _bill = picked;
        _billFileId = null; // a new bill must be uploaded
        _billError = null;
      } else {
        _billError = picked.error;
      }
    });
  }

  Future<void> _pickDate(int maxAgeDays) async {
    final today = ref.read(payrollClockProvider)();
    final t = DateTime(today.year, today.month, today.day);
    final picked = await showDatePicker(
      context: context,
      initialDate: _date ?? t,
      firstDate: t.subtract(Duration(days: maxAgeDays)),
      lastDate: t,
      helpText: 'Expense date',
    );
    if (picked != null && mounted) setState(() => _date = picked);
  }

  Future<void> _submit(ClaimCategories cats) async {
    if (_busy) return;
    setState(() => _error = null);
    final today = ref.read(payrollClockProvider)();
    final formOk = _form.currentState?.validate() ?? false;
    final cat = _category;
    final dateErr = validateExpenseDate(_date, today, cats.maxAgeDays);
    final billMissing = cat != null && cat.billRequired && _bill == null;
    setState(() {
      _submitted = true;
      _billError = billMissing ? 'A bill is required for ${cat.name}' : _billError;
    });
    if (!formOk || cat == null || dateErr != null || billMissing) return;
    final repo = ref.read(payrollRepositoryProvider);
    setState(() => _busy = true);
    try {
      final bill = _bill;
      if (bill != null && _billFileId == null) {
        setState(() => _uploading = true);
        _billFileId = await repo.uploadBill(bill.bytes, bill.name);
        if (mounted) setState(() => _uploading = false);
      }
      await repo.submitClaim(
        category: cat.code,
        expenseDate: ymd(_date!),
        amount: normalizeMoney(_amount.text) ?? _amount.text.trim(),
        description: _desc.text.trim(),
        billFileId: _billFileId,
      );
      ref.invalidate(myClaimsProvider);
      if (!mounted) return;
      payrollSnack(context, 'Claim submitted.');
      Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _uploading = false;
        _busy = false;
        _error = payrollErrorMessage(e);
        if (e.code == 'DUPLICATE_BILL') {
          // The same bill cannot be used again: ask for another one.
          _bill = null;
          _billFileId = null;
        }
        if (e.code == 'VALIDATION_ERROR' && e.detail('bill_file_id') != null) _billFileId = null;
      });
    } finally {
      if (mounted && _busy) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(claimCategoriesProvider);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('New claim')),
        body: async.when(
          skipLoadingOnReload: true,
          loading: () => const LoadingView(label: 'Loading categories'),
          error: (e, _) => ErrorView(
            message: payrollMsg(e),
            onRetry: () => ref.invalidate(claimCategoriesProvider),
          ),
          data: (cats) {
            if (cats.items.isEmpty) {
              return const EmptyView(
                icon: Icons.category_outlined,
                message: 'No claim categories are set up yet. Please contact HR.',
              );
            }
            return _body(context, cats);
          },
        ),
      ),
    );
  }

  Widget _body(BuildContext context, ClaimCategories cats) {
    final theme = Theme.of(context);
    final cat = _category;
    final today = ref.read(payrollClockProvider)();
    final dateErr = _submitted ? validateExpenseDate(_date, today, cats.maxAgeDays) : null;
    return Form(
      key: _form,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 40),
        children: [
          DropdownButtonFormField<String>(
            key: const ValueKey('claim-category'),
            initialValue: cat?.code,
            isExpanded: true,
            decoration: const InputDecoration(labelText: 'Category', border: OutlineInputBorder()),
            items: [for (final c in cats.items) DropdownMenuItem(value: c.code, child: Text(c.name))],
            onChanged: _busy
                ? null
                : (v) => setState(() {
                      _category = cats.byCode(v);
                      _billError = null;
                    }),
            validator: (v) => v == null ? 'Choose a category' : null,
          ),
          if (cat != null)
            Padding(
              padding: const EdgeInsets.only(top: 6, left: 4),
              child: Text(
                [
                  if (cat.maxAmount != null) 'Limit ${formatRupees(cat.maxAmount)}' else 'No limit',
                  cat.billRequired ? 'Bill required' : 'Bill optional',
                ].join('  -  '),
                style: theme.textTheme.bodySmall,
              ),
            ),
          const SizedBox(height: 14),
          InkWell(
            key: const ValueKey('claim-date'),
            onTap: _busy ? null : () => _pickDate(cats.maxAgeDays),
            child: InputDecorator(
              decoration: InputDecoration(
                labelText: 'Expense date',
                border: const OutlineInputBorder(),
                errorText: dateErr,
                helperText: 'Up to ${cats.maxAgeDays} days back',
                suffixIcon: const Icon(Icons.calendar_today_outlined),
              ),
              child: Text(_date == null ? 'Tap to choose' : formatDayLong(_date!)),
            ),
          ),
          const SizedBox(height: 14),
          TextFormField(
            key: const ValueKey('claim-amount'),
            controller: _amount,
            enabled: !_busy,
            keyboardType: const TextInputType.numberWithOptions(decimal: true),
            decoration: const InputDecoration(
              labelText: 'Amount',
              prefixText: '₹ ',
              border: OutlineInputBorder(),
            ),
            validator: (v) => validateClaimAmount(v, cat),
          ),
          const SizedBox(height: 14),
          TextFormField(
            key: const ValueKey('claim-description'),
            controller: _desc,
            enabled: !_busy,
            maxLength: 300,
            maxLines: 3,
            minLines: 2,
            decoration: const InputDecoration(labelText: 'Description', border: OutlineInputBorder()),
            validator: validateClaimDescription,
          ),
          const SizedBox(height: 8),
          Text('Bill', style: theme.textTheme.titleSmall),
          const SizedBox(height: 6),
          if (_bill != null)
            Card(
              child: ListTile(
                key: const ValueKey('claim-bill-chosen'),
                leading: Icon(_bill!.name.toLowerCase().endsWith('.pdf')
                    ? Icons.picture_as_pdf_outlined
                    : Icons.image_outlined,),
                title: Text(_bill!.name, maxLines: 1, overflow: TextOverflow.ellipsis),
                subtitle: Text(_billFileId != null ? 'Uploaded' : 'Will be uploaded when you submit'),
                trailing: IconButton(
                  key: const ValueKey('claim-bill-remove'),
                  tooltip: 'Remove bill',
                  icon: const Icon(Icons.close),
                  onPressed: _busy ? null : () => setState(() {
                        _bill = null;
                        _billFileId = null;
                      }),
                ),
              ),
            )
          else
            OutlinedButton.icon(
              key: const ValueKey('claim-add-bill'),
              onPressed: _busy ? null : _pickBill,
              icon: const Icon(Icons.attach_file),
              label: Text(cat != null && cat.billRequired ? 'Add bill (required)' : 'Add bill'),
            ),
          if (_billError != null)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Semantics(
                liveRegion: true,
                child: Text(_billError!, style: TextStyle(color: theme.colorScheme.error)),
              ),
            ),
          if (_uploading) ...[
            const SizedBox(height: 12),
            const LinearProgressIndicator(),
            const SizedBox(height: 4),
            const Text('Uploading the bill'),
          ],
          if (_error != null)
            Padding(
              padding: const EdgeInsets.only(top: 12),
              child: Semantics(
                liveRegion: true,
                child: Text(_error!, key: const ValueKey('claim-error'), style: TextStyle(color: theme.colorScheme.error)),
              ),
            ),
          const SizedBox(height: 20),
          FilledButton(
            key: const ValueKey('claim-submit'),
            style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
            onPressed: _busy ? null : () => _submit(cats),
            child: _busy
                ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('Submit claim'),
          ),
        ],
      ),
    );
  }
}
