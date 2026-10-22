import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/widgets/state_views.dart';
import '../../attendance/presentation/widgets/summary_card.dart' show StaleBanner;
import '../data/leave_repository.dart';
import '../domain/leave_logic.dart';
import '../domain/leave_models.dart';
import 'leave_providers.dart';
import 'widgets/request_widgets.dart';

class MyRequestsTab extends ConsumerWidget {
  const MyRequestsTab({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final list = ref.watch(myRequestsProvider);
    final filter = ref.watch(requestStatusFilterProvider);

    Future<void> refresh() async {
      ref.invalidate(myRequestsProvider);
      await ref.read(myRequestsProvider.future).then((_) {}, onError: (Object _) {});
    }

    Future<void> cancel(RequestItem item) async {
      Navigator.of(context).pop(); // close the sheet
      final ok = await confirmDialog(
        context,
        title: 'Cancel this request?',
        message: '${item.title}, ${item.whenText}. This cannot be undone.',
        confirmLabel: 'Cancel request',
      );
      if (!ok || !context.mounted) return;
      final messenger = ScaffoldMessenger.of(context);
      try {
        await ref.read(leaveRepositoryProvider).cancel(item.kind, item.id);
        messenger.showSnackBar(const SnackBar(content: Text('Request cancelled.')));
      } on ApiException catch (e) {
        messenger.showSnackBar(SnackBar(content: Text(leaveErrorMessage(e))));
      }
      refreshLeaveData(ref);
    }

    void open(RequestItem item) => showRequestSheet(
          context,
          RequestDetailBody(
            item: item,
            actions: [
              if (item.isPending)
                OutlinedButton.icon(
                  onPressed: () => cancel(item),
                  icon: const Icon(Icons.close),
                  label: const Text('Cancel request'),
                ),
            ],
          ),
        );

    return Column(
      children: [
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          child: Row(
            children: [
              for (final s in <RequestStatus?>[
                null,
                RequestStatus.pending,
                RequestStatus.approved,
                RequestStatus.rejected,
                RequestStatus.cancelled,
              ])
                Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: ChoiceChip(
                    label: Text(s?.label ?? 'All'),
                    selected: filter == s,
                    onSelected: (_) => ref.read(requestStatusFilterProvider.notifier).state = s,
                  ),
                ),
            ],
          ),
        ),
        Expanded(
          child: list.when(
            loading: () => const LoadingView(label: 'Loading requests'),
            error: (e, _) => ErrorView(
              message: e is ApiException ? e.message : 'Could not load your requests.',
              onRetry: refresh,
            ),
            data: (c) {
              final items = [
                for (final i in c.value)
                  if (filter == null || i.status == filter) i,
              ];
              return RefreshIndicator(
                onRefresh: refresh,
                child: items.isEmpty
                    ? ListView(
                        children: [
                          if (c.stale) const Padding(padding: EdgeInsets.all(16), child: StaleBanner()),
                          Padding(
                            padding: const EdgeInsets.only(top: 48),
                            child: EmptyView(
                              message: filter == null
                                  ? 'No requests yet. Use the Request tab to apply.'
                                  : 'No ${filter.label.toLowerCase()} requests.',
                            ),
                          ),
                        ],
                      )
                    : ListView(
                        padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
                        children: [
                          if (c.stale) const StaleBanner(),
                          for (final i in items) RequestTile(item: i, onTap: () => open(i)),
                        ],
                      ),
              );
            },
          ),
        ),
      ],
    );
  }
}
