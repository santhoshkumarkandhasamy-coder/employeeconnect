import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/files/share_confirm.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../auth/presentation/auth_controller.dart';
import '../../auth/presentation/auth_state.dart';
import '../data/food_repository.dart';
import '../domain/food_models.dart';
import 'food_providers.dart';

/// Admin / HR: who needs food on a date. Needs `food.view_all` (the server
/// enforces it too); Excel export additionally needs `export.all|export.team`.
class FoodReportScreen extends ConsumerStatefulWidget {
  const FoodReportScreen({super.key});

  @override
  ConsumerState<FoodReportScreen> createState() => _FoodReportScreenState();
}

class _FoodReportScreenState extends ConsumerState<FoodReportScreen> {
  bool _exporting = false;

  Future<void> _pickDate(String current) async {
    final cur = parseYmd(current) ?? DateTime.now();
    final picked = await showDatePicker(
      context: context,
      initialDate: cur,
      firstDate: DateTime(cur.year - 1),
      lastDate: DateTime(cur.year + 1, 12, 31),
    );
    if (picked != null) ref.read(foodReportDateProvider.notifier).state = ymd(picked);
  }

  Future<void> _export(String date) async {
    if (_exporting) return;
    setState(() => _exporting = true);
    final messenger = ScaffoldMessenger.of(context);
    try {
      final repo = ref.read(foodRepositoryProvider);
      final store = ref.read(exportStoreProvider);
      final opener = ref.read(fileOpenerProvider);
      final confirmed = ref.read(shareConfirmedProvider.notifier);
      final r = await repo.exportXlsx(date);
      if (!mounted) return;
      if (!await confirmShareOnce(context, confirmed)) {
        messenger.showSnackBar(const SnackBar(content: Text('Share cancelled.')));
        return;
      }
      final file = await store.save(r.bytes, r.filename);
      final result = await opener.open(file.path);
      // The share target has its own copy; ours is never kept.
      try {
        if (file.existsSync()) file.deleteSync();
      } on Object {
        // wiped on logout, lock and next start anyway
      }
      if (!mounted) return;
      if (result == OpenResult.dismissed) {
        messenger.showSnackBar(const SnackBar(content: Text('Share cancelled. The file was deleted.')));
      } else if (result == OpenResult.unavailable) {
        messenger.showSnackBar(const SnackBar(
          content: Text('Sharing is not available on this device. The file was deleted.'),
        ),);
      }
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(foodErrorMessage(e))));
    } on Object {
      messenger.showSnackBar(const SnackBar(content: Text('Could not save the file.')));
    } finally {
      if (mounted) setState(() => _exporting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final date = ref.watch(foodReportDateProvider);
    final perms = ref.watch(authControllerProvider
        .select((s) => s is AuthAuthenticated ? s.user.permissions : const <String>[]),);
    final async = ref.watch(foodReportProvider(date));
    final day = parseYmd(date);

    Future<void> refresh() async {
      ref.invalidate(foodReportProvider(date));
      await ref.read(foodReportProvider(date).future).then((_) {}, onError: (Object _) {});
    }

    return Scaffold(
      appBar: AppBar(
        title: const Text('Food report'),
        actions: [
          if (canExportFood(perms))
            _exporting
                ? const Padding(
                    padding: EdgeInsets.all(14),
                    child: SizedBox(
                      width: 20,
                      height: 20,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    ),
                  )
                : IconButton(
                    tooltip: 'Export to Excel',
                    icon: const Icon(Icons.download_outlined),
                    onPressed: () => _export(date),
                  ),
        ],
      ),
      body: Column(
        children: [
          ListTile(
            leading: const Icon(Icons.event),
            title: Text(day == null ? date : formatDayLong(day)),
            subtitle: const Text('Tap to choose a date'),
            trailing: const Icon(Icons.expand_more),
            onTap: () => _pickDate(date),
          ),
          Expanded(
            child: async.when(
              skipLoadingOnRefresh: false,
              loading: () => const LoadingView(label: 'Loading food report'),
              error: (e, _) => ErrorView(
                message: e is ApiException ? foodErrorMessage(e) : 'Could not load the report.',
                onRetry: refresh,
              ),
              data: (r) => RefreshIndicator(
                onRefresh: refresh,
                child: FoodReportBody(report: r),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class FoodReportBody extends StatelessWidget {
  const FoodReportBody({required this.report, super.key});
  final FoodDailyReport report;

  @override
  Widget build(BuildContext context) {
    final groups = groupByDepartment(report.employees);
    final cs = Theme.of(context).colorScheme;
    Widget count(String label, int n, Color color) => Expanded(
          child: Semantics(
            label: '$label: $n',
            excludeSemantics: true,
            child: Column(
              children: [
                Text('$n',
                    style: Theme.of(context)
                        .textTheme
                        .headlineSmall
                        ?.copyWith(color: color, fontWeight: FontWeight.w600),),
                Text(label, style: Theme.of(context).textTheme.bodySmall),
              ],
            ),
          ),
        );
    return ListView(
      physics: const AlwaysScrollableScrollPhysics(),
      padding: const EdgeInsets.fromLTRB(12, 0, 12, 24),
      children: [
        Card(
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 16),
            child: Row(children: [
              count('Need food', report.countYes, cs.primary),
              count('No', report.countNo, cs.onSurface),
              count('Not answered', report.countUnanswered, cs.outline),
            ],),
          ),
        ),
        if (groups.isEmpty)
          const Padding(
            padding: EdgeInsets.only(top: 48),
            child: EmptyView(
              message: 'Nobody has asked for food on this date.',
              icon: Icons.restaurant_outlined,
            ),
          )
        else
          for (final g in groups.entries) ...[
            Padding(
              padding: const EdgeInsets.fromLTRB(4, 16, 4, 4),
              child: Semantics(
                header: true,
                child: Text(
                  '${g.key} (${g.value.length})',
                  style: Theme.of(context).textTheme.titleSmall,
                ),
              ),
            ),
            for (final e in g.value)
              ListTile(
                dense: true,
                contentPadding: const EdgeInsets.symmetric(horizontal: 4),
                leading: const Icon(Icons.restaurant_outlined),
                title: Text(e.person.display),
                subtitle: Text(e.person.employeeId),
                trailing: e.onLeave ? const Chip(label: Text('On leave')) : null,
              ),
          ],
      ],
    );
  }
}
