import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../data/on_duty_repository.dart';
import '../domain/on_duty_models.dart';

/// Active sites assigned to the caller.
final dutyLocationsProvider = FutureProvider.autoDispose<List<DutyLocation>>(
  (ref) => ref.watch(onDutyRepositoryProvider).locations(),
);

final onDutyTodayProvider = FutureProvider.autoDispose<OnDutyToday>(
  (ref) => ref.watch(onDutyRepositoryProvider).today(),
);

/// Visits of one work date (`YYYY-MM-DD`), for the calendar day detail.
final dayVisitsProvider = FutureProvider.autoDispose.family<List<DutyVisit>, String>(
  (ref, date) => ref.watch(onDutyRepositoryProvider).visitsOn(date),
);
