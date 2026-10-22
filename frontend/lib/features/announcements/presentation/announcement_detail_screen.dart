import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/files/file_service.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../domain/announcement_models.dart';
import 'announcement_providers.dart';
import 'announcement_widgets.dart';

/// Detail of one announcement. [initial] (from the list) is shown at once;
/// the fresh copy replaces it. A 404 means deleted or no longer targeted.
/// Opening an unread item marks it read (idempotent on the server).
class AnnouncementDetailScreen extends ConsumerStatefulWidget {
  const AnnouncementDetailScreen({required this.id, super.key, this.initial});
  final String id;
  final Announcement? initial;

  @override
  ConsumerState<AnnouncementDetailScreen> createState() => _AnnouncementDetailScreenState();
}

class _AnnouncementDetailScreenState extends ConsumerState<AnnouncementDetailScreen> {
  bool _marked = false;
  bool _openingPdf = false;

  void _markOnce(Announcement a) {
    if (_marked || a.isRead) return;
    _marked = true;
    final container = ProviderScope.containerOf(context);
    markAnnouncementRead(container, a.id);
  }

  @override
  void initState() {
    super.initState();
    final i = widget.initial;
    if (i != null) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _markOnce(i);
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    ref.listen<AsyncValue<Announcement>>(announcementDetailProvider(widget.id), (_, next) {
      final a = next.valueOrNull;
      if (a != null) _markOnce(a);
    });
    final async = ref.watch(announcementDetailProvider(widget.id));
    final notFound = async.error is ApiException && (async.error! as ApiException).code == 'NOT_FOUND';
    final a = notFound ? null : (async.valueOrNull ?? widget.initial);

    return SecureScreen(
        child: Scaffold(
      appBar: AppBar(title: const Text('Announcement')),
      body: a != null
          ? _Body(
              a: a,
              openingPdf: _openingPdf,
              onOpenPdf: () async {
                setState(() => _openingPdf = true);
                await openAnnouncementPdf(context, ref, a);
                if (mounted) setState(() => _openingPdf = false);
              },
            )
          : async.when(
              loading: () => const LoadingView(label: 'Loading announcement'),
              error: (e, _) => ErrorView(
                message: e is ApiException ? announcementErrorMessage(e) : 'Could not load this announcement.',
                onRetry: notFound ? null : () => ref.invalidate(announcementDetailProvider(widget.id)),
              ),
              data: (_) => const SizedBox.shrink(),
            ),
    ),);
  }
}

class _Body extends ConsumerWidget {
  const _Body({required this.a, required this.openingPdf, required this.onOpenPdf});
  final Announcement a;
  final bool openingPdf;
  final VoidCallback onOpenPdf;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);
    final by = a.createdBy?.display;
    final published = a.publishedAt;
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        if (a.priority != AnnouncementPriority.normal)
          Align(alignment: Alignment.centerLeft, child: PriorityBadge(a.priority)),
        if (a.priority != AnnouncementPriority.normal) const SizedBox(height: 8),
        Semantics(header: true, child: Text(a.title, style: theme.textTheme.headlineSmall)),
        const SizedBox(height: 4),
        Text(
          [
            if (published != null) '${formatDayLong(published)}, ${formatTime(published)}',
            if (by != null && by.isNotEmpty) 'by $by',
          ].join(' - '),
          style: theme.textTheme.bodySmall,
        ),
        if (a.editedLabel(DateTime.now()) case final edited?)
          Text(edited, key: const ValueKey('edited-label'), style: theme.textTheme.bodySmall),
        if (a.expiresAt != null)
          Text('Expires ${formatDayLong(a.expiresAt!)}', style: theme.textTheme.bodySmall),
        const SizedBox(height: 16),
        // Plain text only: never interpreted as HTML or markdown.
        SelectableText(a.body, style: theme.textTheme.bodyLarge),
        if (a.imageUrl != null) ...[
          const SizedBox(height: 16),
          _ImagePreview(url: a.imageUrl!),
        ],
        if (a.pdfUrl != null) ...[
          const SizedBox(height: 16),
          Card(
            child: ListTile(
              leading: const Icon(Icons.picture_as_pdf_outlined),
              title: const Text('PDF attachment'),
              subtitle: const Text('Download and open'),
              trailing: openingPdf
                  ? const SizedBox(width: 24, height: 24, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.download_outlined),
              onTap: openingPdf ? null : onOpenPdf,
            ),
          ),
        ],
      ],
    );
  }
}

class _ImagePreview extends StatelessWidget {
  const _ImagePreview({required this.url});
  final String url;

  @override
  Widget build(BuildContext context) => Semantics(
        button: true,
        label: 'Announcement image. Tap to enlarge.',
        excludeSemantics: true,
        child: GestureDetector(
          onTap: () => showDialog<void>(
            context: context,
            builder: (ctx) => Dialog.fullscreen(
              child: Stack(
                children: [
                  Positioned.fill(
                    child: InteractiveViewer(
                      child: AuthImage(url: url, fit: BoxFit.contain),
                    ),
                  ),
                  Positioned(
                    top: 8,
                    right: 8,
                    child: IconButton.filledTonal(
                      tooltip: 'Close',
                      icon: const Icon(Icons.close),
                      onPressed: () => Navigator.pop(ctx),
                    ),
                  ),
                ],
              ),
            ),
          ),
          child: ClipRRect(
            borderRadius: BorderRadius.circular(12),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxHeight: 360),
              child: SizedBox(
                width: double.infinity,
                child: AuthImage(url: url, fit: BoxFit.cover, height: 240),
              ),
            ),
          ),
        ),
      );
}
