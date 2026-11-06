import 'package:flutter/material.dart';

import '../../../../core/utils/dates.dart';
import '../../domain/leave_models.dart';
import 'attachment_viewer.dart';

/// Status chip with icon + text (colour is never the only cue).
class StatusChip extends StatelessWidget {
  const StatusChip(this.status, {super.key});
  final RequestStatus status;

  @override
  Widget build(BuildContext context) {
    final c = Theme.of(context).colorScheme;
    final (bg, fg, icon) = switch (status) {
      RequestStatus.pending => (c.tertiaryContainer, c.onTertiaryContainer, Icons.hourglass_empty),
      RequestStatus.approved => (c.primaryContainer, c.onPrimaryContainer, Icons.check_circle_outline),
      RequestStatus.rejected => (c.errorContainer, c.onErrorContainer, Icons.cancel_outlined),
      RequestStatus.cancelled => (c.surfaceContainerHighest, c.onSurfaceVariant, Icons.block),
      RequestStatus.unknown => (c.surfaceContainerHighest, c.onSurfaceVariant, Icons.help_outline),
    };
    return Semantics(
      label: 'Status: ${status.label}',
      excludeSemantics: true,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(16)),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 14, color: fg),
            const SizedBox(width: 4),
            Text(status.label, style: Theme.of(context).textTheme.labelMedium?.copyWith(color: fg)),
          ],
        ),
      ),
    );
  }
}

/// One request in a list (My Requests and Approvals).
class RequestTile extends StatelessWidget {
  const RequestTile({required this.item, required this.onTap, super.key, this.showUser = false});
  final RequestItem item;
  final VoidCallback onTap;

  /// Approvals lists show who asked.
  final bool showUser;

  @override
  Widget build(BuildContext context) {
    final who = showUser && item.user != null ? '${item.user!.display}. ' : '';
    final label = '$who${item.title}, ${item.whenText}, ${item.amountText}, ${item.status.label}';
    return Card(
      child: Semantics(
        label: label,
        button: true,
        excludeSemantics: true,
        onTap: onTap,
        child: InkWell(
          borderRadius: BorderRadius.circular(12),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Icon(requestKindIcon(item.kind)),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      if (showUser && item.user != null)
                        Text(
                          '${item.user!.display} (${item.user!.employeeId})',
                          style: Theme.of(context).textTheme.titleSmall,
                        ),
                      Text(
                        '${item.title} - ${item.amountText}',
                        style: showUser && item.user != null
                            ? Theme.of(context).textTheme.bodyMedium
                            : Theme.of(context).textTheme.titleSmall,
                      ),
                      Text(item.whenText, style: Theme.of(context).textTheme.bodySmall),
                    ],
                  ),
                ),
                const SizedBox(width: 8),
                StatusChip(item.status),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// Details shown inside a bottom sheet.
class RequestDetailBody extends StatelessWidget {
  const RequestDetailBody({required this.item, super.key, this.showUser = false, this.actions = const []});
  final RequestItem item;
  final bool showUser;
  final List<Widget> actions;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    Widget row(String k, String v) => Padding(
          padding: const EdgeInsets.symmetric(vertical: 4),
          child: Semantics(
            label: '$k: $v',
            excludeSemantics: true,
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                SizedBox(width: 110, child: Text(k, style: theme.textTheme.labelLarge)),
                Expanded(child: Text(v)),
              ],
            ),
          ),
        );
    return SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          Row(
            children: [
              Expanded(child: Text(item.title, style: theme.textTheme.titleLarge)),
              StatusChip(item.status),
            ],
          ),
          const SizedBox(height: 8),
          if (showUser && item.user != null)
            row('Employee', '${item.user!.display} (${item.user!.employeeId})'
                '${item.user!.department == null ? '' : ', ${item.user!.department}'}'),
          if (item.kind == RequestKind.onDuty && item.clientLocationName != null)
            row(
              'Client location',
              item.clientLocationAddress == null || item.clientLocationAddress!.isEmpty
                  ? item.clientLocationName!
                  : '${item.clientLocationName}, ${item.clientLocationAddress}',
            ),
          row(item.kind.isDateRange ? 'Dates' : 'When', item.whenText),
          row(item.kind.isDateRange ? 'Days' : 'Duration', item.amountText),
          row('Reason', item.reason.isEmpty ? 'Not available (offline or removed after retention)' : item.reason),
          if (item.hasAttachment) ...[
            row('Attachment', 'Attached'),
            AttachmentButton(item: item),
          ],
          if (item.createdAt != null) row('Requested', '${formatDayLong(item.createdAt!)} ${formatTime(item.createdAt!)}'),
          if (item.approver != null && item.status != RequestStatus.pending)
            row('Decided by', item.approver!.display),
          if (item.decidedAt != null) row('Decided', '${formatDayLong(item.decidedAt!)} ${formatTime(item.decidedAt!)}'),
          if (item.decisionComment != null && item.decisionComment!.isNotEmpty)
            row('Comment', item.decisionComment!),
          if (actions.isNotEmpty) ...[
            const SizedBox(height: 16),
            Wrap(spacing: 12, runSpacing: 8, children: actions),
          ],
        ],
      ),
    );
  }
}

Future<void> showRequestSheet(BuildContext context, Widget body) => showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (_) => ConstrainedBox(
        constraints: BoxConstraints(maxHeight: MediaQuery.of(context).size.height * 0.85),
        child: body,
      ),
    );

/// Confirm dialog. Returns true when the user confirms.
Future<bool> confirmDialog(
  BuildContext context, {
  required String title,
  required String message,
  required String confirmLabel,
  String cancelLabel = 'Keep',
}) async =>
    await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(title),
        content: Text(message),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: Text(cancelLabel)),
          FilledButton(onPressed: () => Navigator.pop(ctx, true), child: Text(confirmLabel)),
        ],
      ),
    ) ??
    false;

IconData requestKindIcon(RequestKind k) => switch (k) {
      RequestKind.leave => Icons.beach_access_outlined,
      RequestKind.permission => Icons.timer_outlined,
      RequestKind.onDuty => Icons.business_center_outlined,
    };
