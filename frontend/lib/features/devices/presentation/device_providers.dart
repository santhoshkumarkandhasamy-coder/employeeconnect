import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../admin/presentation/admin_providers.dart' show AdminPagedNotifier, adminMsg;
import '../../announcements/presentation/announcement_providers.dart' show PagedList;
import '../../attendance/domain/attendance_models.dart' show Paged;
import '../data/device_repository.dart';
import '../domain/device_models.dart';

export '../../admin/presentation/admin_providers.dart' show adminMsg;

/// True only in the web admin console: devices can be added and edited there
/// (it keeps communication keys off phones). The console overrides this.
final deviceEditAllowedProvider = Provider<bool>((ref) => false);

final devicesProvider = FutureProvider.autoDispose<List<Device>>(
  (ref) => ref.watch(deviceRepositoryProvider).devices(),
);

final deviceDetailProvider = FutureProvider.autoDispose.family<Device, String>(
  (ref, id) => ref.watch(deviceRepositoryProvider).device(id),
);

class DeviceUsersNotifier extends AdminPagedNotifier<DeviceUser, DeviceUserQuery> {
  @override
  Future<Paged<DeviceUser>> fetchPage(DeviceUserQuery arg, int page) =>
      ref.read(deviceRepositoryProvider).deviceUsers(arg, page: page);
  @override
  String idOf(DeviceUser item) => item.id;
}

final deviceUsersProvider = AsyncNotifierProvider.autoDispose
    .family<DeviceUsersNotifier, PagedList<DeviceUser>, DeviceUserQuery>(DeviceUsersNotifier.new);

class DevicePunchesNotifier extends AdminPagedNotifier<DevicePunch, PunchQuery> {
  @override
  Future<Paged<DevicePunch>> fetchPage(PunchQuery arg, int page) =>
      ref.read(deviceRepositoryProvider).punches(arg, page: page);
  @override
  String idOf(DevicePunch item) => item.id;
}

final devicePunchesProvider = AsyncNotifierProvider.autoDispose
    .family<DevicePunchesNotifier, PagedList<DevicePunch>, PunchQuery>(DevicePunchesNotifier.new);

/// The caller's own punches for one day (`YYYY-MM-DD`).
final myPunchesProvider = FutureProvider.autoDispose.family<List<MyPunch>, String>(
  (ref, date) => ref.watch(deviceRepositoryProvider).myPunches(date: date),
);

String deviceMsg(Object e) => adminMsg(e);
