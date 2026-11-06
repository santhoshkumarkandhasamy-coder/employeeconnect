import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/widgets/state_views.dart';
import '../../features/admin/domain/admin_models.dart' show AdminPerm;
import '../../features/admin/domain/ops_models.dart';
import '../../features/admin/presentation/admin_providers.dart';
import '../console_auth.dart';

/// Console dashboard (`GET /admin/dashboard`). Every block the caller may not
/// see arrives as null and is hidden. Refreshes every 60 s while the tab is
/// visible.
class DashboardPage extends ConsumerStatefulWidget {
  const DashboardPage({super.key, this.refreshEvery = const Duration(seconds: 60)});
  final Duration refreshEvery;

  @override
  ConsumerState<DashboardPage> createState() => _DashboardState();
}

class _DashboardState extends ConsumerState<DashboardPage> with WidgetsBindingObserver {
  Timer? _timer;
  bool _visible = true;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _timer = Timer.periodic(widget.refreshEvery, (_) {
      if (_visible && mounted) _refresh();
    });
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _timer?.cancel();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _visible = state == AppLifecycleState.resumed;
    if (_visible && mounted) _refresh();
  }

  void _refresh() {
    ref.invalidate(dashboardProvider);
    if (ref.read(consolePermissionsProvider).contains(AdminPerm.systemView)) {
      ref.invalidate(systemHealthProvider);
    }
  }

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(dashboardProvider);
    final canHealth = ref.watch(consolePermissionsProvider).contains(AdminPerm.systemView);
    return async.when(
      skipLoadingOnReload: true,
      loading: () => const LoadingView(),
      error: (e, _) => ErrorView(message: adminMsg(e), onRetry: _refresh),
      data: (d) => RefreshIndicator(
        onRefresh: () async => _refresh(),
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Text('Today${d.date == null ? '' : ' (${d.date})'}',
                style: Theme.of(context).textTheme.titleLarge,),
            const SizedBox(height: 12),
            Wrap(
              spacing: 12,
              runSpacing: 12,
              children: [
                if (d.seats != null) _seatsCard(context, d.seats!),
                if (d.employees != null)
                  _card('Employees', {
                    'Active': d.employees!['active'],
                    'Inactive': d.employees!['inactive'],
                  }),
                if (d.attendance != null)
                  _card("Today's attendance", {
                    'Checked in': d.attendance!['checked_in'],
                    'Late': d.attendance!['late'],
                    'On leave': d.attendance!['on_leave'],
                    'Not checked in': d.attendance!['not_checked_in'],
                  }),
                if (d.requests != null)
                  _card('Pending requests', {
                    'Leave': d.requests!['leave_pending'],
                    'Permission': d.requests!['permission_pending'],
                    'Total': d.pendingRequests,
                  }),
                if (d.sessions != null)
                  _card('Live sessions', {
                    'Mobile': d.sessions!['mobile'],
                    'Web': d.sessions!['web'],
                  }),
                if (d.resetOpen != null)
                  _card('Password resets', {'Open requests': d.resetOpen}),
              ],
            ),
            if (canHealth) ...[
              const SizedBox(height: 24),
              Text('System health', style: Theme.of(context).textTheme.titleLarge),
              const SizedBox(height: 8),
              const HealthPanel(),
            ],
          ],
        ),
      ),
    );
  }

  Widget _card(String title, Map<String, int?> rows) => SizedBox(
        width: 260,
        child: Card(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(title, style: Theme.of(context).textTheme.titleMedium),
                const SizedBox(height: 8),
                for (final e in rows.entries)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 2),
                    child: Row(
                      mainAxisAlignment: MainAxisAlignment.spaceBetween,
                      children: [Text(e.key), Text('${e.value ?? 0}')],
                    ),
                  ),
              ],
            ),
          ),
        ),
      );

  Widget _seatsCard(BuildContext context, DashboardSeats s) => SizedBox(
        width: 260,
        child: Card(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Seats', style: Theme.of(context).textTheme.titleMedium),
                const SizedBox(height: 8),
                Text('${s.used} / ${s.total}', style: Theme.of(context).textTheme.headlineSmall),
                const SizedBox(height: 8),
                LinearProgressIndicator(
                  value: s.total == 0 ? 0 : (s.used / s.total).clamp(0, 1).toDouble(),
                ),
                const SizedBox(height: 8),
                Chip(label: Text(licenseStatusLabel(s.status))),
              ],
            ),
          ),
        ),
      );
}

/// `GET /admin/system/health` (`system.view`).
class HealthPanel extends ConsumerWidget {
  const HealthPanel({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(systemHealthProvider);
    return async.when(
      skipLoadingOnReload: true,
      loading: () => const Padding(padding: EdgeInsets.all(16), child: LoadingView()),
      error: (e, _) => ErrorView(
        message: adminMsg(e),
        onRetry: () => ref.invalidate(systemHealthProvider),
      ),
      data: (h) => Card(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Icon(
                    h.degraded ? Icons.warning_amber_rounded : Icons.check_circle_outline,
                    color: h.degraded ? Colors.orange : Colors.green,
                  ),
                  const SizedBox(width: 8),
                  Text(h.degraded ? 'Degraded' : 'All systems OK'),
                  if (h.appVersion != null) ...[const Spacer(), Text('v${h.appVersion}')],
                ],
              ),
              const SizedBox(height: 8),
              Wrap(
                spacing: 8,
                children: [
                  for (final e in h.checks.entries)
                    Chip(
                      avatar: Icon(e.value ? Icons.check : Icons.close, size: 16),
                      label: Text(e.key),
                    ),
                ],
              ),
              if (h.tlsDaysLeft != null) Text('TLS certificate: ${h.tlsDaysLeft} days left'),
              if (h.uploadsFreePct != null) Text('Uploads disk free: ${h.uploadsFreePct}%'),
            ],
          ),
        ),
      ),
    );
  }
}
