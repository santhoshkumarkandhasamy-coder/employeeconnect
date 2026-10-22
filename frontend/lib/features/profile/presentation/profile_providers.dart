import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/cache/json_cache.dart';
import '../data/profile_repository.dart';
import '../domain/profile_models.dart';

class ProfileNotifier extends AutoDisposeAsyncNotifier<Cached<Profile>> {
  ProfileRepository get _repo => ref.read(profileRepositoryProvider);

  @override
  Future<Cached<Profile>> build() => ref.watch(profileRepositoryProvider).profile();

  /// Saves the changed keys. Throws `ApiException` (caller shows it).
  Future<void> save(Map<String, Object?> body) async {
    if (body.isEmpty) return;
    final p = await _repo.update(body);
    state = AsyncData(Cached(p));
  }

  /// Uploads a new photo; reloads when the server does not return the profile.
  Future<void> changePhoto(String path, {String? filename}) async {
    final p = await _repo.uploadPhoto(path, filename: filename);
    if (p != null) {
      state = AsyncData(Cached(p));
    } else {
      state = AsyncData(await _repo.profile());
    }
  }

  Future<void> reload() async {
    ref.invalidateSelf();
    await future.then((_) {}, onError: (Object _) {});
  }
}

final profileProvider =
    AsyncNotifierProvider.autoDispose<ProfileNotifier, Cached<Profile>>(ProfileNotifier.new);
