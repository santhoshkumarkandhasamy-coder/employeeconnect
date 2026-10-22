import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/files/file_service.dart';
import '../../food/data/food_repository.dart' show OpenResult, exportStoreProvider, fileOpenerProvider;
import '../domain/announcement_models.dart';

/// Colour-coded priority chip. Normal priority shows nothing in lists (see
/// [AnnouncementTile]); the label text is always present, so colour is never
/// the only signal.
class PriorityBadge extends StatelessWidget {
  const PriorityBadge(this.priority, {super.key});
  final AnnouncementPriority priority;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final (Color bg, Color fg, IconData icon) = switch (priority) {
      AnnouncementPriority.urgent => (cs.errorContainer, cs.onErrorContainer, Icons.priority_high),
      AnnouncementPriority.important =>
        (cs.tertiaryContainer, cs.onTertiaryContainer, Icons.info_outline),
      AnnouncementPriority.normal =>
        (cs.surfaceContainerHighest, cs.onSurfaceVariant, Icons.circle_outlined),
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(12)),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 14, color: fg),
          const SizedBox(width: 4),
          Text(
            priority.label,
            style: Theme.of(context).textTheme.labelSmall?.copyWith(color: fg),
          ),
        ],
      ),
    );
  }
}

/// One row of the announcements list.
class AnnouncementTile extends StatelessWidget {
  const AnnouncementTile({required this.item, required this.now, required this.onTap, super.key});
  final Announcement item;
  final DateTime now;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final when = relativeTime(item.publishedAt, now);
    final label = '${item.isRead ? '' : 'Unread. '}${item.priority.label} priority. '
        '${item.title}. $when';
    return Semantics(
      button: true,
      label: label,
      excludeSemantics: true,
      child: Card(
        margin: const EdgeInsets.symmetric(vertical: 4),
        child: InkWell(
          borderRadius: BorderRadius.circular(12),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Padding(
                  padding: const EdgeInsets.only(top: 6, right: 10),
                  child: item.isRead
                      ? const SizedBox(width: 10, height: 10)
                      : Container(
                          key: const ValueKey('unread-dot'),
                          width: 10,
                          height: 10,
                          decoration: BoxDecoration(
                            color: theme.colorScheme.primary,
                            shape: BoxShape.circle,
                          ),
                        ),
                ),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        item.title,
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: theme.textTheme.titleMedium?.copyWith(
                          fontWeight: item.isRead ? FontWeight.w400 : FontWeight.w700,
                        ),
                      ),
                      const SizedBox(height: 4),
                      // The offline copy has no body (scrubbed from the disk cache).
                      if (item.body.isNotEmpty) ...[
                        Text(
                          item.body,
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: theme.textTheme.bodyMedium,
                        ),
                        const SizedBox(height: 8),
                      ],
                      Wrap(
                        spacing: 8,
                        runSpacing: 4,
                        crossAxisAlignment: WrapCrossAlignment.center,
                        children: [
                          if (item.priority != AnnouncementPriority.normal)
                            PriorityBadge(item.priority),
                          if (item.hasAttachment)
                            Icon(Icons.attach_file, size: 16, color: theme.colorScheme.outline),
                          Text(when, style: theme.textTheme.bodySmall),
                        ],
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// True when [bytes] start with the PDF magic number.
bool looksLikePdf(List<int> bytes) =>
    bytes.length > 4 && bytes[0] == 0x25 && bytes[1] == 0x50 && bytes[2] == 0x44 && bytes[3] == 0x46;

/// Safe local file name for an announcement PDF (never from the server).
String pdfFileName(String announcementId) =>
    'announcement-${announcementId.replaceAll(RegExp(r'[^A-Za-z0-9-]'), '_')}.pdf';

/// Warns that the PDF leaves the app (OS share sheet) and asks to continue.
/// True when the user agrees. Review 07 L7.
Future<bool> confirmShareLeavesApp(BuildContext context) async {
  final ok = await showDialog<bool>(
    context: context,
    builder: (ctx) => AlertDialog(
      title: const Text('Open outside the app?'),
      content: const Text(
        'This PDF will be handed to another app through the share sheet. It may '
        'contain company information, and this app can no longer protect it once '
        'it is shared. Only continue if you trust the app you choose, and open '
        'PDFs only from announcements you expect.',
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
        FilledButton(
          onPressed: () => Navigator.pop(ctx, true),
          style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
          child: const Text('Continue'),
        ),
      ],
    ),
  );
  return ok ?? false;
}

/// Downloads the PDF through the authenticated client, saves it to the
/// app-private export folder (wiped on logout / lock and at start) and hands
/// it to the file opener. Same pattern as the food export.
Future<void> openAnnouncementPdf(BuildContext context, WidgetRef ref, Announcement a) async {
  final messenger = ScaffoldMessenger.of(context);
  // Resolve everything before the first await: the screen may be closed while
  // the download runs and `ref` must not be used after that.
  final files = ref.read(fileServiceProvider);
  final store = ref.read(exportStoreProvider);
  final opener = ref.read(fileOpenerProvider);
  try {
    final bytes = await files.bytes(a.pdfUrl!);
    if (!looksLikePdf(bytes)) {
      messenger.showSnackBar(const SnackBar(content: Text('This file is not a valid PDF.')));
      return;
    }
    if (!context.mounted) return;
    // Sharing hands the file to other apps, outside this app's protection.
    if (!await confirmShareLeavesApp(context)) return;
    final file = await store.save(bytes, pdfFileName(a.id));
    final opened = await opener.open(file.path);
    if (!context.mounted || opened != OpenResult.unavailable) return;
    await showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('PDF saved'),
        content: Text(
          'The PDF was saved in the app\'s private storage:\n\n${file.path}\n\n'
          'It is deleted when you log out or the app locks.',
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Close')),
        ],
      ),
    );
  } on ApiException catch (e) {
    messenger.showSnackBar(SnackBar(content: Text(announcementErrorMessage(e))));
  } on Object {
    messenger.showSnackBar(const SnackBar(content: Text('Could not open the PDF.')));
  }
}
