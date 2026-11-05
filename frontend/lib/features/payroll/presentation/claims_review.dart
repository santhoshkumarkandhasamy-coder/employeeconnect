import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart' show AdminPagedBody, adminSelfIdProvider;
import '../../leave/domain/leave_logic.dart' show RequestPushScreen, RequestPushTarget;
import '../data/payroll_repository.dart';
import '../domain/claims_models.dart';
import '../domain/payroll_models.dart' show canManagePayroll, canOpenPayroll;
import '../domain/payroll_money.dart';
import 'claims_widgets.dart';
import 'pay_items_widgets.dart';
import 'payroll_providers.dart';
import 'my_claims_screen.dart';
import 'payroll_widgets.dart';

/// Which step the reviewer is on: the manager (verify / reject) or HR
/// (approve / reject / move payout month).
///
/// [auto] (a push opened the claim) offers whatever fits the claim's state and
/// the user's rights.
enum ClaimStage { manager, hr, auto }

/// Detail of one team / company claim with the actions that fit [stage]. The
/// server enforces every rule again; stale-state answers (`NOT_PENDING`,
/// `VERSION_CONFLICT`, `ITEM_LOCKED`) reload the claim.
class ClaimReviewScreen extends ConsumerStatefulWidget {
  const ClaimReviewScreen({required this.claimId, required this.stage, super.key, this.canManage = false});
  final String claimId;
  final ClaimStage stage;

  /// Holds `payroll.manage` (HR stage actions).
  final bool canManage;

  @override
  ConsumerState<ClaimReviewScreen> createState() => _ClaimReviewScreenState();
}

class _ClaimReviewScreenState extends ConsumerState<ClaimReviewScreen> {
  bool _busy = false;

  void _reload() {
    ref.invalidate(adminClaimProvider(widget.claimId));
    ref.invalidate(adminClaimsProvider);
  }

  Future<void> _run(String done, Future<void> Function(PayrollRepository repo) call) async {
    if (_busy) return;
    final repo = ref.read(payrollRepositoryProvider);
    setState(() => _busy = true);
    await payGuard(context, () async {
      await call(repo);
      if (mounted) payrollSnack(context, done);
      return true;
    });
    _reload();
    if (mounted) setState(() => _busy = false);
  }

  Future<void> _verify(Claim c) async {
    final comment = await showClaimCommentDialog(
      context,
      title: 'Verify this claim?',
      label: 'Comment (optional)',
      confirmLabel: 'Verify',
      required: false,
      message: 'You confirm the expense is genuine. HR approves the amount next.',
    );
    if (comment == null || !mounted) return;
    await _run('Claim verified.', (r) => r.verifyClaim(c.id, comment: comment));
  }

  Future<void> _reject(Claim c) async {
    final comment = await showClaimCommentDialog(
      context,
      title: 'Reject this claim?',
      label: 'Reason for rejecting',
      confirmLabel: 'Reject',
      required: true,
    );
    if (comment == null || !mounted) return;
    await _run('Claim rejected.', (r) => r.rejectClaim(c.id, comment: comment));
  }

  Future<void> _approve(Claim c) async {
    final thisMonth = monthKey(ref.read(payrollClockProvider)());
    final r = await showDialog<ClaimApproveResult>(
      context: context,
      builder: (_) => ClaimApproveDialog(claim: c, thisMonth: thisMonth),
    );
    if (r == null || !mounted) return;
    await _run(
      'Claim approved.',
      (repo) => repo.approveClaim(
        c.id,
        approvedAmount: r.amount,
        period: r.period,
        comment: r.comment,
        expectedVersion: c.version,
      ),
    );
  }

  Future<void> _payout(Claim c) async {
    final thisMonth = monthKey(ref.read(payrollClockProvider)());
    final period = await showDialog<String>(
      context: context,
      builder: (_) => ClaimPayoutDialog(claim: c, thisMonth: thisMonth),
    );
    if (period == null || !mounted) return;
    await _run(
      'Payout month changed.',
      (repo) => repo.payoutClaim(c.id, period: period, expectedVersion: c.version),
    );
  }

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(adminClaimProvider(widget.claimId));
    final selfId = ref.watch(adminSelfIdProvider);
    final hr = widget.stage != ClaimStage.manager;
    final mgr = widget.stage != ClaimStage.hr;
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('Claim')),
        body: async.when(
          skipLoadingOnReload: true,
          loading: () => const LoadingView(label: 'Loading claim'),
          error: (e, _) => ErrorView(
            message: payrollMsg(e),
            onRetry: () => ref.invalidate(adminClaimProvider(widget.claimId)),
          ),
          data: (c) {
            final own = selfId != null && c.user?.id == selfId;
            final canVerify = mgr && c.awaitingManager && !own;
            final canReject = !own &&
                ((hr &&
                        widget.canManage &&
                        (c.status == ClaimStatus.submitted || c.status == ClaimStatus.verified)) ||
                    (mgr && c.awaitingManager));
            final canApprove = hr && widget.canManage && !own && c.awaitingHr;
            final canPayout = hr && widget.canManage && !own && c.isApproved;
            return ListView(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
              children: [
                ClaimDetailBody(claim: c, admin: true),
                if (own) const Text('You cannot decide on your own claim.'),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    if (canVerify)
                      FilledButton.icon(
                        key: const ValueKey('claim-verify'),
                        onPressed: _busy ? null : () => _verify(c),
                        icon: const Icon(Icons.verified_outlined),
                        label: const Text('Verify'),
                      ),
                    if (canApprove)
                      FilledButton.icon(
                        key: const ValueKey('claim-approve'),
                        onPressed: _busy ? null : () => _approve(c),
                        icon: const Icon(Icons.check),
                        label: const Text('Approve'),
                      ),
                    if (canPayout)
                      OutlinedButton.icon(
                        key: const ValueKey('claim-payout'),
                        onPressed: _busy ? null : () => _payout(c),
                        icon: const Icon(Icons.event_repeat_outlined),
                        label: const Text('Move payout month'),
                      ),
                    if (canReject)
                      OutlinedButton.icon(
                        key: const ValueKey('claim-reject'),
                        onPressed: _busy ? null : () => _reject(c),
                        icon: const Icon(Icons.close),
                        label: const Text('Reject'),
                      ),
                  ],
                ),
              ],
            );
          },
        ),
      ),
    );
  }
}

class ClaimApproveResult {
  const ClaimApproveResult({required this.amount, required this.period, this.comment});
  final String amount;
  final String period;
  final String? comment;
}

/// Approve: amount (up to the claimed amount), payout month, optional comment.
class ClaimApproveDialog extends StatefulWidget {
  const ClaimApproveDialog({required this.claim, required this.thisMonth, super.key});
  final Claim claim;
  final String thisMonth;

  @override
  State<ClaimApproveDialog> createState() => _ClaimApproveDialogState();
}

class _ClaimApproveDialogState extends State<ClaimApproveDialog> {
  final _form = GlobalKey<FormState>();
  late final _amount = TextEditingController(text: widget.claim.amount);
  final _comment = TextEditingController();
  late String? _period = widget.claim.period ?? widget.thisMonth;

  @override
  void dispose() {
    _amount.dispose();
    _comment.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: const Text('Approve claim'),
        content: SingleChildScrollView(
          child: Form(
            key: _form,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text('Claimed ${formatRupees(widget.claim.amount)}'),
                const SizedBox(height: 12),
                MoneyField(
                  controller: _amount,
                  label: 'Approved amount',
                  positive: true,
                ),
                const SizedBox(height: 12),
                MonthDropdown(
                  label: 'Pay with the payroll of',
                  value: _period,
                  options: monthOptionsWith(widget.thisMonth, widget.claim.period, back: 0),
                  onChanged: (v) => setState(() => _period = v),
                ),
                const SizedBox(height: 12),
                TextFormField(
                  key: const ValueKey('claim-comment'),
                  controller: _comment,
                  maxLength: 200,
                  decoration: const InputDecoration(
                    labelText: 'Comment (optional)',
                    border: OutlineInputBorder(),
                  ),
                  validator: (v) => validateClaimComment(v, required: false),
                ),
              ],
            ),
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            key: const ValueKey('claim-approve-ok'),
            onPressed: () {
              if (!(_form.currentState?.validate() ?? false)) return;
              final err = validateApprovedAmount(_amount.text, widget.claim.amount);
              if (err != null) {
                ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(err)));
                return;
              }
              Navigator.pop(
                context,
                ClaimApproveResult(
                  amount: normalizeMoney(_amount.text) ?? _amount.text.trim(),
                  period: _period!,
                  comment: _comment.text.trim().isEmpty ? null : _comment.text.trim(),
                ),
              );
            },
            child: const Text('Approve'),
          ),
        ],
      );
}

/// Move an approved claim to another payout month.
class ClaimPayoutDialog extends StatefulWidget {
  const ClaimPayoutDialog({required this.claim, required this.thisMonth, super.key});
  final Claim claim;
  final String thisMonth;

  @override
  State<ClaimPayoutDialog> createState() => _ClaimPayoutDialogState();
}

class _ClaimPayoutDialogState extends State<ClaimPayoutDialog> {
  late String? _period = widget.claim.period;

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: const Text('Move payout month'),
        content: MonthDropdown(
          label: 'Pay with the payroll of',
          value: _period,
          options: monthOptionsWith(widget.thisMonth, widget.claim.period),
          onChanged: (v) => setState(() => _period = v),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            key: const ValueKey('claim-payout-ok'),
            onPressed: _period == null || _period == widget.claim.period
                ? null
                : () => Navigator.pop(context, _period),
            child: const Text('Move'),
          ),
        ],
      );
}

/// Paged list of claims for [query]; tapping one opens [ClaimReviewScreen].
class AdminClaimsList extends ConsumerWidget {
  const AdminClaimsList({
    required this.query,
    required this.stage,
    required this.emptyMessage,
    super.key,
    this.canManage = false,
    this.header,
  });
  final AdminClaimQuery query;
  final ClaimStage stage;
  final String emptyMessage;
  final bool canManage;
  final Widget? header;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(adminClaimsProvider(query));
    return AdminPagedBody<Claim>(
      async: async,
      emptyMessage: emptyMessage,
      emptyIcon: Icons.receipt_long_outlined,
      header: header,
      onRefresh: () async => ref.read(adminClaimsProvider(query).notifier).reload(),
      onLoadMore: () => ref.read(adminClaimsProvider(query).notifier).loadMore(),
      itemBuilder: (context, c) => ClaimTile(
        claim: c,
        showUser: true,
        onTap: () async {
          await Navigator.of(context).push<void>(
            MaterialPageRoute<void>(
              builder: (_) => ClaimReviewScreen(claimId: c.id, stage: stage, canManage: canManage),
            ),
          );
          ref.invalidate(adminClaimsProvider);
        },
      ),
    );
  }
}

/// "Claims" segment of Approvals for managers: direct reports' submitted claims.
class ManagerClaimsBody extends StatelessWidget {
  const ManagerClaimsBody({super.key});

  static const query = AdminClaimQuery(stage: 'manager');

  @override
  Widget build(BuildContext context) => const AdminClaimsList(
        query: query,
        stage: ClaimStage.manager,
        emptyMessage: 'No claims waiting for your verification.',
      );
}

/// "Claims" segment of Pay items (HR / admin): the HR queue, or every claim.
class ClaimsSection extends ConsumerStatefulWidget {
  const ClaimsSection({required this.canManage, super.key});
  final bool canManage;

  @override
  ConsumerState<ClaimsSection> createState() => _ClaimsSectionState();
}

class _ClaimsSectionState extends ConsumerState<ClaimsSection> {
  bool _queue = true;
  String? _status;

  @override
  Widget build(BuildContext context) {
    final q = _queue
        ? const AdminClaimQuery(stage: 'hr')
        : AdminClaimQuery(stage: 'all', status: _status);
    return AdminClaimsList(
      query: q,
      stage: ClaimStage.hr,
      canManage: widget.canManage,
      emptyMessage: _queue ? 'No claims waiting for HR.' : 'No claims found.',
      header: Padding(
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
        child: Wrap(
          spacing: 8,
          runSpacing: 8,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            ChoiceChip(
              key: const ValueKey('claims-queue'),
              label: const Text('Waiting for HR'),
              selected: _queue,
              onSelected: (_) => setState(() => _queue = true),
            ),
            ChoiceChip(
              key: const ValueKey('claims-all'),
              label: const Text('All claims'),
              selected: !_queue,
              onSelected: (_) => setState(() => _queue = false),
            ),
            if (!_queue)
              SizedBox(
                width: 170,
                child: DropdownButtonFormField<String?>(
                  key: const ValueKey('claims-status'),
                  initialValue: _status,
                  isExpanded: true,
                  decoration: const InputDecoration(
                    labelText: 'Status',
                    isDense: true,
                    border: OutlineInputBorder(),
                  ),
                  items: [
                    const DropdownMenuItem<String?>(child: Text('All')),
                    for (final s in [...claimFilterStatuses, 'processing'])
                      DropdownMenuItem<String?>(value: s, child: Text(claimStatusLabel(s))),
                  ],
                  onChanged: (v) => setState(() => _status = v),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

/// Screen for a claim push (`reimbursement_submitted` / `reimbursement_decision`,
/// contract 17.7), or null when the push is not about claims or the user may
/// not open it. A decision opens the employee's own claim; a submission opens
/// the claim for the manager (team claims) or, failing that, for HR.
Widget? claimPushScreen(RequestPushTarget target, List<String> perms) {
  final id = target.id;
  switch (target.screen) {
    case RequestPushScreen.myClaims:
      if (!canSubmitClaims(perms)) return null;
      return id == null ? const MyClaimsScreen() : ClaimDetailScreen(claimId: id);
    case RequestPushScreen.claimApprovals:
      if (id == null) return null;
      if (!canSeeTeamClaims(perms) && !canOpenPayroll(perms)) return null;
      return ClaimReviewScreen(claimId: id, stage: ClaimStage.auto, canManage: canManagePayroll(perms));
    case RequestPushScreen.approvals:
    case RequestPushScreen.myRequests:
      return null;
  }
}
