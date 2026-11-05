import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/widgets/state_views.dart';
import '../../admin/presentation/admin_widgets.dart' show adminPermissionsProvider, adminSelfIdProvider;
import '../../payroll/domain/claims_models.dart' show canSeeTeamClaims;
import '../../payroll/presentation/claims_review.dart' show ManagerClaimsBody;
import '../domain/leave_logic.dart';
import '../domain/leave_models.dart';
import 'leave_providers.dart';
import 'widgets/request_widgets.dart';

/// Approvals for managers / HR / admins. Which requests the server returns is
/// decided by the caller's permissions (team vs all); the UI only hides what
/// the user cannot use.
class ApprovalsScreen extends ConsumerStatefulWidget {
  const ApprovalsScreen({super.key});

  @override
  ConsumerState<ApprovalsScreen> createState() => _ApprovalsScreenState();
}

class _ApprovalsScreenState extends ConsumerState<ApprovalsScreen> {
  RequestKind _kind = RequestKind.leave;

  /// The Claims segment (expense reimbursements, manager step) is showing.
  bool _claims = false;
  RequestStatus _status = RequestStatus.pending;

  ApprovalsQuery get _query => (kind: _kind, status: _status);

  @override
  Widget build(BuildContext context) {
    // The admin providers read the mobile session by default and the console
    // session when overridden, so this screen works in both.
    final selfId = ref.watch(adminSelfIdProvider);
    final perms = ref.watch(adminPermissionsProvider);
    final canDecide = canDecideRequests(perms);
    final canClaims = canSeeTeamClaims(perms);
    final async = ref.watch(approvalsProvider(_query));

    Future<void> refresh() async {
      ref.invalidate(approvalsProvider(_query));
      await ref.read(approvalsProvider(_query).future).then((_) {}, onError: (Object _) {});
    }

    Future<void> decide(RequestItem item, {required bool approve}) async {
      final comment = await showDecisionDialog(context, item: item, approve: approve);
      if (comment == null || !mounted || !context.mounted) return;
      final messenger = ScaffoldMessenger.of(context);
      try {
        await ref.read(approvalsProvider(_query).notifier).decide(item, approve: approve, comment: comment);
        messenger.showSnackBar(SnackBar(content: Text(approve ? 'Request approved.' : 'Request rejected.')));
      } on ApiException catch (e) {
        messenger.showSnackBar(SnackBar(content: Text(leaveErrorMessage(e))));
      }
    }

    void open(RequestItem item) {
      final own = selfId != null && item.user?.id == selfId;
      showRequestSheet(
        context,
        RequestDetailBody(
          item: item,
          showUser: true,
          actions: [
            if (item.isPending && canDecide && !own) ...[
              FilledButton.icon(
                onPressed: () {
                  Navigator.of(context).pop();
                  decide(item, approve: true);
                },
                icon: const Icon(Icons.check),
                label: const Text('Approve'),
              ),
              OutlinedButton.icon(
                onPressed: () {
                  Navigator.of(context).pop();
                  decide(item, approve: false);
                },
                icon: const Icon(Icons.close),
                label: const Text('Reject'),
              ),
            ],
            if (item.isPending && own)
              const Text('You cannot approve or reject your own request.'),
          ],
        ),
      );
    }

    return Scaffold(
      appBar: AppBar(title: const Text('Approvals')),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 0),
            child: SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              child: SegmentedButton<String>(
                showSelectedIcon: false,
                segments: [
                  const ButtonSegment(value: 'leave', label: Text('Leave')),
                  const ButtonSegment(value: 'permission', label: Text('Permission')),
                  const ButtonSegment(value: 'on_duty', label: Text('On duty')),
                  if (canClaims) const ButtonSegment(value: 'claims', label: Text('Claims')),
                ],
                selected: {_claims ? 'claims' : _kind.name == 'onDuty' ? 'on_duty' : _kind.name},
                onSelectionChanged: (s) => setState(() {
                  final v = s.first;
                  _claims = v == 'claims';
                  if (!_claims) {
                    _kind = v == 'leave'
                        ? RequestKind.leave
                        : v == 'permission'
                            ? RequestKind.permission
                            : RequestKind.onDuty;
                  }
                }),
              ),
            ),
          ),
          if (_claims)
            const Expanded(child: ManagerClaimsBody())
          else ...[
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            child: Row(
              children: [
                for (final s in [
                  RequestStatus.pending,
                  RequestStatus.approved,
                  RequestStatus.rejected,
                  RequestStatus.cancelled,
                ])
                  Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: ChoiceChip(
                      label: Text(s.label),
                      selected: _status == s,
                      onSelected: (_) => setState(() => _status = s),
                    ),
                  ),
              ],
            ),
          ),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            child: Align(
              alignment: Alignment.centerLeft,
              child: Text(
                'Showing: ${approvalScopeLabel(perms)}',
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ),
          ),
          Expanded(
            child: async.when(
              loading: () => const LoadingView(label: 'Loading requests'),
              error: (e, _) => ErrorView(
                message: e is ApiException
                    ? (e.code == 'FORBIDDEN' ? 'You do not have access to approvals.' : e.message)
                    : 'Could not load requests.',
                onRetry: refresh,
              ),
              data: (s) => RefreshIndicator(
                onRefresh: refresh,
                child: s.items.isEmpty
                    ? ListView(children: [
                        Padding(
                          padding: const EdgeInsets.only(top: 48),
                          child: EmptyView(
                            icon: Icons.task_alt,
                            message: _status == RequestStatus.pending
                                ? 'Nothing waiting for approval.'
                                : 'No ${_status.label.toLowerCase()} requests.',
                          ),
                        ),
                      ],)
                    : ListView(
                        padding: const EdgeInsets.all(16),
                        children: [
                          for (final i in s.items) RequestTile(item: i, showUser: true, onTap: () => open(i)),
                          if (s.loadMoreError != null)
                            Padding(
                              padding: const EdgeInsets.all(8),
                              child: Text(s.loadMoreError!, textAlign: TextAlign.center),
                            ),
                          if (s.hasMore)
                            Padding(
                              padding: const EdgeInsets.all(8),
                              child: Center(
                                child: s.loadingMore
                                    ? const CircularProgressIndicator()
                                    : OutlinedButton(
                                        onPressed: () =>
                                            ref.read(approvalsProvider(_query).notifier).loadMore(),
                                        child: const Text('Load more'),
                                      ),
                              ),
                            ),
                        ],
                      ),
              ),
            ),
          ),
          ],
        ],
      ),
    );
  }
}

/// Asks for the decision comment. Returns the comment ('' when approving
/// without one) or null when cancelled. Rejecting requires a comment.
Future<String?> showDecisionDialog(
  BuildContext context, {
  required RequestItem item,
  required bool approve,
}) =>
    showDialog<String>(
      context: context,
      builder: (_) => DecisionDialog(item: item, approve: approve),
    );

class DecisionDialog extends StatefulWidget {
  const DecisionDialog({required this.item, required this.approve, super.key});
  final RequestItem item;
  final bool approve;

  @override
  State<DecisionDialog> createState() => _DecisionDialogState();
}

class _DecisionDialogState extends State<DecisionDialog> {
  final _key = GlobalKey<FormState>();
  final _comment = TextEditingController();

  @override
  void dispose() {
    _comment.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final approve = widget.approve;
    final who = widget.item.user?.display ?? 'this employee';
    return AlertDialog(
      title: Text(approve ? 'Approve request?' : 'Reject request?'),
      content: Form(
        key: _key,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('$who, ${widget.item.title}, ${widget.item.whenText}'),
            if (approve && widget.item.kind == RequestKind.leave && widget.item.onDutyConflict) ...[
              const SizedBox(height: 8),
              Text(
                'This employee has an approved On Duty request on these dates. If you approve '
                'the leave, they will not be able to check in on those days.',
                style: TextStyle(color: Theme.of(context).colorScheme.error),
              ),
            ],
            const SizedBox(height: 12),
            TextFormField(
              controller: _comment,
              maxLines: 3,
              maxLength: 500,
              decoration: InputDecoration(
                labelText: approve ? 'Comment (optional)' : 'Reason for rejecting',
                border: const OutlineInputBorder(),
              ),
              validator: (v) => !approve && (v ?? '').trim().isEmpty ? 'A comment is required to reject' : null,
            ),
          ],
        ),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
        FilledButton(
          onPressed: () {
            if (_key.currentState?.validate() ?? false) Navigator.pop(context, _comment.text.trim());
          },
          child: Text(approve ? 'Approve' : 'Reject'),
        ),
      ],
    );
  }
}
