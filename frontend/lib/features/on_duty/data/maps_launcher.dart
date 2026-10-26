import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:share_plus/share_plus.dart';

import '../domain/on_duty_models.dart';

/// Hands the coordinates of a SITE to a maps app. Only the site is ever sent;
/// the employee's own position is never included.
abstract class MapsLauncher {
  /// Returns false when nothing could be opened.
  Future<bool> open(DutyLocation site);
}

/// Uses the OS share sheet (already a dependency for exports) with a maps link,
/// so no extra plugin or native query configuration is needed. The user taps
/// the link in their maps app of choice.
class ShareMapsLauncher implements MapsLauncher {
  const ShareMapsLauncher();

  @override
  Future<bool> open(DutyLocation site) async {
    try {
      await Share.share(mapsLinkFor(site.lat, site.lng), subject: site.name);
      return true;
    } on Object {
      return false;
    }
  }
}

final mapsLauncherProvider = Provider<MapsLauncher>((_) => const ShareMapsLauncher());
