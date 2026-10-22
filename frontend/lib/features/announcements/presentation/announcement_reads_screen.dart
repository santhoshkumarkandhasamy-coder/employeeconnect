import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/files/file_service.dart' show UserAvatar;
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../data/announcement_repository.dart';
import '../domain/announcement_models.dart';
import 'announcement_providers.dart';

/// Who has read an announcement (`GET /admin/announcements/{id}/reads`).
class AnnouncementReadsScreen extends ConsumerStatefulWidget {
  const AnnouncementReadsScreen({required this.announcement, super.key});
  final Announcement announcement;

  @override
  ConsumerState<AnnouncementReadsScreen> createState() => _AnnouncementReadsScreenState();
}

class _AnnouncementReadsScreenState extends ConsumerState<AnnouncementReadsScreen> {
  final List<ReaderItem> _more = [];
  int _page = 1;
  bool _hasMoreExtra = false;
  bool _loading = false;
  String? _error;
  bool _extraInit = false;

  Future<void> _loadMore(ReadStats first) async {
    if (_loading) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final r = await ref
          .read(announcementRepositoryProvider)
          .reads(widget.announcement.id, page: _page + 1);
      if (!mounted) return;
      setState(() {
        _more.addAll(r.readers);
        _page = r.page;
        _hasMoreExtra = r.hasMore;
        _loading = false;
      });
    } on ApiException catch (e) {
      if (mounted) {
        setState(() {
          _loading = false;
          _error = announcementErrorMessage(e);
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final id = widget.announcement.id;
    final async = ref.watch(readStatsProvider(id));
    return Scaffold(
      appBar: AppBar(title: const Text('Read statistics')),
      body: async.when(
        loading: () => const LoadingView(label: 'Loading read statistics'),
        error: (e, _) => ErrorView(
          message: e is ApiException ? announcementErrorMessage(e) : 'Could not load statistics.',
          onRetry: () => ref.invalidate(readStatsProvider(id)),
        ),
        data: (s) {
          if (!_extraInit) {
            _extraInit = true;
            _page = s.page;
            _hasMoreExtra = s.hasMore;
          }
          final readers = [...s.readers, ..._more];
          return RefreshIndicator(
            onRefresh: () async {
              setState(() {
                _more.clear();
                _extraInit = false;
              });
              ref.invalidate(readStatsProvider(id));
              await ref.read(readStatsProvider(id).future).then((_) {}, onError: (Object _) {});
            },
            child: ListView(
              physics: const AlwaysScrollableScrollPhysics(),
              padding: const EdgeInsets.all(16),
              children: [
                Text(widget.announcement.title, style: Theme.of(context).textTheme.titleMedium),
                const SizedBox(height: 12),
                Semantics(
                  label: '${s.readCount} of ${s.targetCount} read',
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      LinearProgressIndicator(value: s.fraction, minHeight: 8),
                      const SizedBox(height: 6),
                      Text('${s.readCount} of ${s.targetCount} read'),
                    ],
                  ),
                ),
                const SizedBox(height: 16),
                if (readers.isEmpty)
                  const EmptyView(icon: Icons.visibility_off_outlined, message: 'Nobody has read this yet.')
                else
                  for (final r in readers)
                    ListTile(
                      contentPadding: EdgeInsets.zero,
                      leading: UserAvatar(name: r.user.display),
                      title: Text(r.user.display),
                      subtitle: Text([
                        if (r.user.department != null) r.user.department!,
                        if (r.readAt != null) '${formatDayLong(r.readAt!)}, ${formatTime(r.readAt!)}',
                      ].join(' - '),),
                    ),
                if (_error != null) Text(_error!, textAlign: TextAlign.center),
                if (_hasMoreExtra)
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: OutlinedButton(
                      onPressed: _loading ? null : () => _loadMore(s),
                      child: Text(_loading ? 'Loading' : 'Load more'),
                    ),
                  ),
              ],
            ),
          );
        },
      ),
    );
  }
}
