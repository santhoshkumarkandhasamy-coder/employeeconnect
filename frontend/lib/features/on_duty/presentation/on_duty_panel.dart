import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../attendance/data/location_service.dart';
import '../../attendance/presentation/attendance_providers.dart';
import '../data/maps_launcher.dart';
import '../domain/on_duty_models.dart';
import 'on_duty_providers.dart';
import 'on_duty_request_form.dart';

/// Asks before the first OS location prompt. Returns true to continue.
Future<bool> showOnDutyRationale(BuildContext context) async {
  final ok = await showDialog<bool>(
    context: context,
    builder: (ctx) => AlertDialog(
      title: const Text('Allow location access?'),
      content: const Text(onDutyRationale),
      actions: [
        TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Not now')),
        FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Continue')),
      ],
    ),
  );
  return ok ?? false;
}

/// The On Duty half of the Today tab: choose a site, check in, check out,
/// and see today's visits. Location is read once per punch (and once when the
/// panel opens or is refreshed); there is no live tracking.
class OnDutyPanel extends ConsumerStatefulWidget {
  const OnDutyPanel({super.key});

  @override
  ConsumerState<OnDutyPanel> createState() => _OnDutyPanelState();
}

class _OnDutyPanelState extends ConsumerState<OnDutyPanel> {
  final _note = TextEditingController();

  @override
  void dispose() {
    _note.dispose();
    super.dispose();
  }

  Future<void> _checkIn(DutyLocation site) async {
    final cfg = ref.read(onDutyConfigProvider);
    final ok = await ref.read(visitControllerProvider.notifier).checkIn(
          locationId: site.id,
          locationName: site.name,
          selfieRequired: cfg.selfieRequired,
          confirmRationale: () => showOnDutyRationale(context),
          note: _note.text,
        );
    if (ok) _note.clear();
  }

  Future<void> _checkOut(DutyVisit visit) async {
    final ok = await ref.read(visitControllerProvider.notifier).checkOut(
          visitId: visit.id,
          locationName: visit.locationName,
          confirmRationale: () => showOnDutyRationale(context),
          note: _note.text,
        );
    if (ok) _note.clear();
  }

  @override
  Widget build(BuildContext context) {
    final today = ref.watch(onDutyTodayProvider);
    return today.when(
      loading: () => const Padding(padding: EdgeInsets.all(32), child: LoadingView(label: 'Loading On Duty')),
      error: (e, _) => ErrorView(
        message: e is ApiException ? onDutyErrorMessage(e) : 'Could not load On Duty.',
        onRetry: () => ref.invalidate(onDutyTodayProvider),
      ),
      data: (t) => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (t.openVisit != null) _openVisit(t.openVisit!) else ..._checkInSection(t),
          const _PunchFeedback(),
          if (t.visits.isNotEmpty) ...[
            const SizedBox(height: 12),
            VisitTimeline(visits: t.visits),
          ],
        ],
      ),
    );
  }

  // ---- open visit --------------------------------------------------------------

  Widget _openVisit(DutyVisit v) {
    final s = ref.watch(visitControllerProvider);
    final theme = Theme.of(context);
    final since = v.checkInAt == null ? '' : ' since ${formatTime(v.checkInAt!)}';
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Card(
          color: theme.colorScheme.primaryContainer,
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Semantics(
              liveRegion: true,
              child: Row(
                children: [
                  Icon(Icons.place, color: theme.colorScheme.onPrimaryContainer),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Text(
                      'On duty at ${v.locationName}$since',
                      style: theme.textTheme.titleMedium
                          ?.copyWith(color: theme.colorScheme.onPrimaryContainer),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
        const SizedBox(height: 12),
        _noteField('Note for this visit (optional)'),
        const SizedBox(height: 12),
        FilledButton.icon(
          style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(64)),
          onPressed: s.busy ? null : () => _checkOut(v),
          icon: s.busy
              ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
              : const Icon(Icons.logout),
          label: Text(
            s.busy ? _busyLabel(s.step, out: true) : 'Check out',
            style: theme.textTheme.titleMedium,
          ),
        ),
      ],
    );
  }

  Widget _noteField(String label) => TextField(
        controller: _note,
        enabled: !ref.watch(visitControllerProvider).busy,
        maxLength: maxVisitNoteLength,
        maxLines: 2,
        minLines: 1,
        textInputAction: TextInputAction.done,
        decoration: InputDecoration(labelText: label, border: const OutlineInputBorder()),
      );

  String _busyLabel(CheckStep step, {required bool out}) => switch (step) {
        CheckStep.locating => 'Getting location',
        CheckStep.selfie => 'Waiting for selfie',
        CheckStep.uploading => 'Uploading selfie',
        CheckStep.submitting => out ? 'Checking out' : 'Checking in',
        CheckStep.idle => out ? 'Check out' : 'Check in here',
      };

  // ---- check in ----------------------------------------------------------------

  List<Widget> _checkInSection(OnDutyToday t) {
    final sites = ref.watch(dutyLocationsProvider);
    final prox = ref.watch(proximityProvider);
    final cfg = ref.watch(onDutyConfigProvider);
    final s = ref.watch(visitControllerProvider);
    final theme = Theme.of(context);
    final reason = blockedReasonText(t.blockedReason);
    final fix = prox.valueOrNull?.fix;

    return [
      if (reason != null)
        _Notice(
          icon: t.blockedReason == 'ON_DUTY_NOT_APPROVED' ? Icons.how_to_reg_outlined : Icons.info_outline,
          text: reason,
          color: theme.colorScheme.tertiaryContainer,
          action: t.blockedReason == 'ON_DUTY_NOT_APPROVED'
              ? FilledButton.tonal(
                  onPressed: () => Navigator.of(context).push<void>(
                    MaterialPageRoute<void>(builder: (_) => const OnDutyRequestScreen()),
                  ),
                  child: const Text('Request On Duty'),
                )
              : null,
        ),
      if (t.approval.required && t.approval.covered && t.blockedReason == null)
        _Notice(
          icon: Icons.verified_outlined,
          text: t.approval.anyLocation
              ? 'Approved for today at any assigned site.'
              : 'Approved for today at selected sites only.',
          color: theme.colorScheme.secondaryContainer,
        ),
      _LocationStatus(prox: prox),
      const SizedBox(height: 8),
      sites.when(
        loading: () => const Padding(padding: EdgeInsets.all(24), child: LoadingView(label: 'Loading sites')),
        error: (e, _) => ErrorView(
          message: e is ApiException ? onDutyErrorMessage(e) : 'Could not load your sites.',
          onRetry: () => ref.invalidate(dutyLocationsProvider),
        ),
        data: (list) {
          if (list.isEmpty) {
            return const EmptyView(
              icon: Icons.place_outlined,
              message: 'No on-duty locations are assigned to you.',
            );
          }
          final sorted = sortByDistance(list, lat: fix?.lat, lng: fix?.lng);
          final picked = ref.watch(selectedSiteProvider);
          final selected = sorted.any((e) => e.location.id == picked) ? picked : sorted.first.location.id;
          final site = sorted.firstWhere((e) => e.location.id == selected).location;
          final needsApproval = t.approval.blocks(site.id);
          final canGo = t.canCheckIn && !needsApproval && !s.busy;
          return Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              for (final e in sorted)
                _SiteTile(
                  entry: e,
                  selected: e.location.id == selected,
                  enabled: !s.busy,
                  onTap: () => ref.read(selectedSiteProvider.notifier).state = e.location.id,
                ),
              if (needsApproval && t.canCheckIn)
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Text(
                    'Your approved request does not cover ${site.name}. Pick another site or request approval.',
                    style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.error),
                  ),
                ),
              const SizedBox(height: 12),
              _noteField('Note (optional)'),
              if (cfg.selfieRequired || s.selfieNeeded) ...[
                const SizedBox(height: 12),
                const _SelfieRow(),
              ],
              const SizedBox(height: 12),
              Semantics(
                button: true,
                label: s.busy ? _busyLabel(s.step, out: false) : 'Check in here, ${site.name}',
                excludeSemantics: true,
                child: FilledButton.icon(
                  style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(64)),
                  onPressed: canGo ? () => _checkIn(site) : null,
                  icon: s.busy
                      ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                      : const Icon(Icons.login),
                  label: Text(
                    s.busy ? _busyLabel(s.step, out: false) : 'Check in here',
                    style: theme.textTheme.titleMedium,
                  ),
                ),
              ),
              if (t.visitsRemaining > 0 && t.visitsRemaining < 4)
                Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: Text(
                    '${t.visitsRemaining} ${t.visitsRemaining == 1 ? 'visit' : 'visits'} left today.',
                    style: theme.textTheme.bodySmall,
                    textAlign: TextAlign.center,
                  ),
                ),
            ],
          );
        },
      ),
    ];
  }
}

/// One-shot position status for the panel (no distance to any boundary).
class _LocationStatus extends ConsumerWidget {
  const _LocationStatus({required this.prox});
  final AsyncValue<Proximity> prox;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);
    final (IconData icon, String text, bool bad) = prox.when(
      loading: () => (Icons.my_location, 'Finding your location', false),
      error: (_, __) => (Icons.location_disabled, 'Could not read your location.', true),
      data: (p) => switch (p.access) {
        LocationAccess.serviceOff => (Icons.location_disabled, 'Location services are off.', true),
        LocationAccess.denied => (
            Icons.location_disabled,
            'Location permission is asked when you check in.',
            false,
          ),
        LocationAccess.deniedForever => (
            Icons.location_disabled,
            'Location permission is blocked in Settings.',
            true,
          ),
        LocationAccess.reducedAccuracy => (Icons.location_searching, preciseLocationMessage, true),
        LocationAccess.granted => p.fix == null
            ? (Icons.location_searching, 'Location unavailable right now.', true)
            : (Icons.my_location, 'Location found. Sites are sorted nearest first.', false),
      },
    );
    return Card(
      color: bad ? theme.colorScheme.errorContainer : null,
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Row(
          children: [
            Icon(icon),
            const SizedBox(width: 12),
            Expanded(child: Semantics(liveRegion: true, child: Text(text))),
            if (prox.valueOrNull?.access == LocationAccess.reducedAccuracy)
              TextButton(
                onPressed: () =>
                    ref.read(locationServiceProvider).openSettings(LocationAccess.reducedAccuracy),
                child: const Text('Open settings'),
              ),
            IconButton(
              tooltip: 'Refresh location',
              icon: const Icon(Icons.refresh),
              onPressed: () => ref.invalidate(proximityProvider),
            ),
          ],
        ),
      ),
    );
  }
}

class _SiteTile extends ConsumerWidget {
  const _SiteTile({
    required this.entry,
    required this.selected,
    required this.enabled,
    required this.onTap,
  });
  final SiteDistance entry;
  final bool selected;
  final bool enabled;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);
    final site = entry.location;
    final dist = entry.meters == null ? null : formatDistance(entry.meters!);
    final sub = [
      if (site.address != null) site.address!,
      if (dist != null) '$dist away',
    ].join('  -  ');
    return Card(
      shape: selected
          ? RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(12),
              side: BorderSide(color: theme.colorScheme.primary, width: 2),
            )
          : null,
      child: Semantics(
        selected: selected,
        button: true,
        label: '${site.name}${entry.nearby ? ', nearby' : ''}${dist == null ? '' : ', $dist away'}',
        excludeSemantics: true,
        onTap: enabled ? onTap : null,
        child: InkWell(
          borderRadius: BorderRadius.circular(12),
          onTap: enabled ? onTap : null,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(12, 8, 4, 8),
            child: Row(
              children: [
                Icon(selected ? Icons.radio_button_checked : Icons.radio_button_unchecked),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Wrap(
                        spacing: 8,
                        crossAxisAlignment: WrapCrossAlignment.center,
                        children: [
                          Text(site.name, style: theme.textTheme.titleSmall),
                          if (entry.nearby)
                            Container(
                              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                              decoration: BoxDecoration(
                                color: theme.colorScheme.primaryContainer,
                                borderRadius: BorderRadius.circular(12),
                              ),
                              child: Text(
                                'Nearby',
                                style: theme.textTheme.labelSmall
                                    ?.copyWith(color: theme.colorScheme.onPrimaryContainer),
                              ),
                            ),
                        ],
                      ),
                      if (sub.isNotEmpty) Text(sub, style: theme.textTheme.bodySmall),
                    ],
                  ),
                ),
                IconButton(
                  tooltip: 'Open ${site.name} in Maps',
                  icon: const Icon(Icons.map_outlined),
                  onPressed: () async {
                    final ok = await ref.read(mapsLauncherProvider).open(site);
                    if (!ok && context.mounted) {
                      ScaffoldMessenger.of(context).showSnackBar(
                        const SnackBar(content: Text('Could not open a maps app.')),
                      );
                    }
                  },
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _SelfieRow extends ConsumerWidget {
  const _SelfieRow();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final path = ref.watch(visitControllerProvider.select((s) => s.selfiePath));
    final busy = ref.watch(visitControllerProvider.select((s) => s.busy));
    final ctl = ref.read(visitControllerProvider.notifier);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Row(
          children: [
            if (path != null)
              ClipRRect(
                borderRadius: BorderRadius.circular(8),
                child: Image.file(
                  File(path),
                  width: 56,
                  height: 56,
                  fit: BoxFit.cover,
                  errorBuilder: (_, __, ___) => const Icon(Icons.broken_image_outlined, size: 56),
                ),
              )
            else
              const Icon(Icons.face_retouching_natural, size: 40),
            const SizedBox(width: 12),
            const Expanded(child: Text('Selfie required')),
            TextButton(
              onPressed: busy ? null : ctl.pickSelfie,
              child: Text(path == null ? 'Take selfie' : 'Retake'),
            ),
          ],
        ),
      ),
    );
  }
}

class _Notice extends StatelessWidget {
  const _Notice({required this.icon, required this.text, required this.color, this.action});
  final IconData icon;
  final String text;
  final Color color;
  final Widget? action;

  @override
  Widget build(BuildContext context) => Container(
        margin: const EdgeInsets.only(bottom: 12),
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(color: color, borderRadius: BorderRadius.circular(12)),
        child: Semantics(
          liveRegion: true,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(icon),
                  const SizedBox(width: 8),
                  Expanded(child: Text(text)),
                ],
              ),
              if (action != null) ...[const SizedBox(height: 8), action!],
            ],
          ),
        ),
      );
}

/// Result of the last punch, plus the mock-location notice.
class _PunchFeedback extends ConsumerWidget {
  const _PunchFeedback();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final s = ref.watch(visitControllerProvider);
    final theme = Theme.of(context);
    return Column(
      children: [
        if (s.mockLocation)
          Padding(
            padding: const EdgeInsets.only(top: 12),
            child: _Notice(
              icon: Icons.warning_amber_rounded,
              text: 'A mock location app seems to be active. This is reported to your employer.',
              color: theme.colorScheme.tertiaryContainer,
            ),
          ),
        if (s.error != null)
          Padding(
            padding: const EdgeInsets.only(top: 12),
            child: _Notice(
              icon: Icons.error_outline,
              text: s.error!,
              color: theme.colorScheme.errorContainer,
              action: s.blockedAccess == null
                  ? null
                  : TextButton(
                      onPressed: () => ref.read(locationServiceProvider).openSettings(s.blockedAccess!),
                      child: const Text('Open settings'),
                    ),
            ),
          ),
        if (s.success != null)
          Padding(
            padding: const EdgeInsets.only(top: 12),
            child: _Notice(
              icon: Icons.check_circle_outline,
              text: s.success!,
              color: theme.colorScheme.primaryContainer,
            ),
          ),
      ],
    );
  }
}

/// Today's visits, oldest first.
class VisitTimeline extends StatelessWidget {
  const VisitTimeline({required this.visits, super.key, this.title = "Today's visits"});
  final List<DutyVisit> visits;
  final String title;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(title, style: theme.textTheme.titleSmall),
            const SizedBox(height: 8),
            for (final v in visits)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 4),
                child: Semantics(
                  label: '${v.locationName}, ${v.timeText}${v.note == null ? '' : ', note ${v.note}'}',
                  excludeSemantics: true,
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Icon(
                        v.open ? Icons.radio_button_checked : Icons.check_circle_outline,
                        size: 18,
                        color: v.autoClosed ? theme.colorScheme.error : null,
                      ),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(v.locationName, style: theme.textTheme.bodyMedium),
                            Text(
                              v.timeText,
                              style: theme.textTheme.bodySmall?.copyWith(
                                color: v.autoClosed ? theme.colorScheme.error : null,
                              ),
                            ),
                            if (v.note != null) Text(v.note!, style: theme.textTheme.bodySmall),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
