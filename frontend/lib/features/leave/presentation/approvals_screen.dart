import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/widgets/state_views.dart';
import '../../auth/presentation/auth_controller.dart';
import '../../auth/presentation/auth_state.dart';
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
  RequestStatus _status = RequestStatus.pending;

  ApprovalsQuery get _query => (kind: _kind, status: _status);

  @override
  Widget build(BuildContext context) {
    final user = ref.watch(authControllerProvider.select((s) => s is AuthAuthenticated ? s.user : null));
    final perms = user?.permissions ?? const <String>[];
    final canDecide = canDecideRequests(perms);
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
      final own = user != null && item.user?.id == user.id;
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
            child: SegmentedButton<RequestKind>(
              segments: const [
                ButtonSegment(value: RequestKind.leave, label: Text('Leave')),
                ButtonSegment(value: RequestKind.permission, label: Text('Permission')),
                ButtonSegment(value: RequestKind.onDuty, label: Text('On duty')),
              ],
              selected: {_kind},
              onSelectionChanged: (s) => setState(() => _kind = s.first),
            ),
          ),
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
