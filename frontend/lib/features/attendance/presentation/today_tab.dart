import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../data/location_service.dart';
import '../domain/attendance_models.dart';
import 'attendance_providers.dart';
import 'widgets/today_status_card.dart';

class TodayTab extends ConsumerWidget {
  const TodayTab({super.key});

  Future<bool> _rationale(BuildContext context) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Allow location access?'),
        content: const Text(
          'Employee Connect uses your location only at the moment you check in or '
          'out, to confirm you are at the office. It is not tracked in the background.',
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Not now')),
          FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Continue')),
        ],
      ),
    );
    return ok ?? false;
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final today = ref.watch(todayAttendanceProvider);
    return today.when(
      loading: () => const LoadingView(label: 'Loading today'),
      error: (e, _) => ErrorView(
        message: e is ApiException ? e.message : 'Could not load today.',
        onRetry: () => ref.invalidate(todayAttendanceProvider),
      ),
      data: (rec) => RefreshIndicator(
        onRefresh: () async {
          ref
            ..invalidate(todayAttendanceProvider)
            ..invalidate(proximityProvider);
          await ref.read(todayAttendanceProvider.future).then((_) {}, onError: (Object _) {});
        },
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            _StatusBlock(record: rec),
            const SizedBox(height: 12),
            const _ProximityCard(),
            const SizedBox(height: 12),
            if (rec == null) const _SelfieSection(),
            const _FeedbackBanner(),
            const SizedBox(height: 16),
            _ActionButton(record: rec, onRationale: () => _rationale(context)),
          ],
        ),
      ),
    );
  }
}

class _StatusBlock extends StatelessWidget {
  const _StatusBlock({required this.record});
  final AttendanceRecord? record;

  @override
  Widget build(BuildContext context) {
    final r = record;
    final theme = Theme.of(context);
    final now = DateTime.now();
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(formatDayLong(now), style: theme.textTheme.titleMedium),
            const SizedBox(height: 8),
            Text(todayStatusText(r), style: theme.textTheme.bodyLarge),
            if (r != null && r.shiftStart != null)
              Text('Shift ${r.shiftStart} - ${r.shiftEnd ?? ''}', style: theme.textTheme.bodySmall),
            if (r != null && r.workedMinutes != null)
              Text('Worked ${formatDuration(r.workedMinutes!)}', style: theme.textTheme.bodySmall),
            if (r != null && r.autoClosed)
              Text(
                'No check-out was recorded for this day.',
                style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.error),
              ),
          ],
        ),
      ),
    );
  }
}

class _ProximityCard extends ConsumerWidget {
  const _ProximityCard();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final prox = ref.watch(proximityProvider);
    final theme = Theme.of(context);
    final (IconData icon, String text, bool bad) = prox.when(
      loading: () => (Icons.my_location, 'Finding your location', false),
      error: (_, __) => (Icons.location_disabled, 'Could not read your location.', true),
      data: (p) {
        switch (p.access) {
          case LocationAccess.serviceOff:
            return (Icons.location_disabled, 'Location services are off.', true);
          case LocationAccess.denied:
            return (Icons.location_disabled, 'Location permission not granted yet.', false);
          case LocationAccess.deniedForever:
            return (Icons.location_disabled, 'Location permission is blocked in Settings.', true);
          case LocationAccess.reducedAccuracy:
            return (Icons.location_searching, preciseLocationMessage, true);
          case LocationAccess.granted:
            if (p.fix == null) return (Icons.location_searching, 'Location unavailable right now.', true);
            final inside = p.inside;
            if (inside == null) return (Icons.my_location, 'Location found.', false);
            // Coarse on purpose: no exact distance is shown.
            return (
              inside ? Icons.location_on : Icons.wrong_location,
              inside ? 'You are inside the office area.' : 'You are outside the office area.',
              !inside,
            );
        }
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
                onPressed: () => ref
                    .read(locationServiceProvider)
                    .openSettings(LocationAccess.reducedAccuracy),
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

class _SelfieSection extends ConsumerWidget {
  const _SelfieSection();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final cfg = ref.watch(attendanceConfigProvider).valueOrNull;
    final path = ref.watch(checkControllerProvider.select((s) => s.selfiePath));
    final needed = ref.watch(checkControllerProvider.select((s) => s.selfieNeeded));
    final busy = ref.watch(checkControllerProvider.select((s) => s.busy));
    final required = (cfg?.selfieRequired ?? false) || needed;
    final ctl = ref.read(checkControllerProvider.notifier);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Row(
          children: [
            if (path != null)
              Semantics(
                label: 'Selfie preview',
                image: true,
                child: ClipRRect(
                  borderRadius: BorderRadius.circular(8),
                  child: Image.file(
                    File(path),
                    width: 56,
                    height: 56,
                    fit: BoxFit.cover,
                    errorBuilder: (_, __, ___) => const Icon(Icons.broken_image_outlined, size: 56),
                  ),
                ),
              )
            else
              const Icon(Icons.face_retouching_natural, size: 40),
            const SizedBox(width: 12),
            Expanded(child: Text(required ? 'Selfie required' : 'Selfie (optional)')),
            TextButton(
              onPressed: busy ? null : ctl.pickSelfie,
              child: Text(path == null ? 'Take selfie' : 'Retake'),
            ),
            if (path != null && !required)
              IconButton(
                tooltip: 'Remove selfie',
                icon: const Icon(Icons.close),
                onPressed: busy ? null : ctl.clearSelfie,
              ),
          ],
        ),
      ),
    );
  }
}

class _FeedbackBanner extends ConsumerWidget {
  const _FeedbackBanner();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final s = ref.watch(checkControllerProvider);
    final theme = Theme.of(context);
    final children = <Widget>[
      if (s.mockLocation)
        _note(
          context,
          Icons.warning_amber_rounded,
          'A mock location app seems to be active. This is reported to your employer.',
          theme.colorScheme.tertiaryContainer,
        ),
      if (s.error != null)
        _note(context, Icons.error_outline, s.error!, theme.colorScheme.errorContainer,
            action: s.blockedAccess == null
                ? null
                : TextButton(
                    onPressed: () =>
                        ref.read(locationServiceProvider).openSettings(s.blockedAccess!),
                    child: const Text('Open settings'),
                  ),),
      if (s.success != null)
        _note(context, Icons.check_circle_outline, s.success!, theme.colorScheme.primaryContainer),
    ];
    return Column(children: children);
  }

  Widget _note(BuildContext context, IconData icon, String text, Color bg, {Widget? action}) =>
      Container(
        margin: const EdgeInsets.only(top: 12),
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(12)),
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
              if (action != null) action,
            ],
          ),
        ),
      );
}

class _ActionButton extends ConsumerWidget {
  const _ActionButton({required this.record, required this.onRationale});
  final AttendanceRecord? record;
  final Future<bool> Function() onRationale;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final s = ref.watch(checkControllerProvider);
    final r = record;
    final done = r != null && r.checkedOut;
    final checkOut = r != null && r.checkedIn && !r.checkedOut;
    final label = done
        ? 'Done for today'
        : switch (s.step) {
            CheckStep.locating => 'Getting location',
            CheckStep.selfie => 'Waiting for selfie',
            CheckStep.uploading => 'Uploading selfie',
            CheckStep.submitting => checkOut ? 'Checking out' : 'Checking in',
            CheckStep.idle => checkOut ? 'Check out' : 'Check in',
          };
    return Semantics(
      button: true,
      label: label,
      excludeSemantics: true,
      child: FilledButton.icon(
        style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(64)),
        onPressed: done || s.busy
            ? null
            : () => ref
                .read(checkControllerProvider.notifier)
                .submit(checkOut: checkOut, confirmRationale: onRationale),
        icon: s.busy
            ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
            : Icon(checkOut ? Icons.logout : Icons.login),
        label: Text(label, style: Theme.of(context).textTheme.titleMedium),
      ),
    );
  }
}
