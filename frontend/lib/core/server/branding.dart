import 'dart:async';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/dio_client.dart';
import '../config/constants.dart';
import '../storage/app_state_store.dart';
import 'company.dart';

/// Cached company branding (name + logo). Loaded from secure storage at
/// start, refreshed from `/config`, `/auth/me` or `/public/company` whenever
/// the name or `logo_version` changed. Survives a wipe; "Change server"
/// clears it.
class BrandingNotifier extends Notifier<BrandingData> {
  @override
  BrandingData build() {
    unawaited(_load());
    return BrandingData.empty;
  }

  Future<void> _load() async {
    try {
      final d = await ref.read(appStateStoreProvider).readBranding();
      if (state.isEmpty) state = d;
    } on Object {
      // no stored branding
    }
  }

  /// Applies [c]; downloads the logo only when its version changed.
  Future<void> update(CompanyInfo c) async {
    final cur = state;
    final hasLogo = c.logoUrl != null;
    var logo = hasLogo ? cur.logo : null;
    if (hasLogo && (cur.logo == null || cur.logoVersion != c.logoVersion)) {
      try {
        final res = await ref.read(dioProvider).get<List<int>>(
              '/public/company/logo',
              queryParameters: {'v': c.logoVersion},
              options: Options(
                responseType: ResponseType.bytes,
                extra: const {noAuthKey: true},
              ),
            );
        final b = res.data;
        if (b != null && b.isNotEmpty) logo = Uint8List.fromList(b);
      } on DioException {
        // keep the previous logo; try again next time
      }
    }
    final next = BrandingData(name: c.name, logoVersion: c.logoVersion, logo: logo);
    if (next.name == cur.name && next.logoVersion == cur.logoVersion && next.logo == cur.logo) {
      return;
    }
    state = next;
    try {
      await ref.read(appStateStoreProvider).writeBranding(next);
    } on Object {
      // best effort
    }
  }

  Future<void> clear() async {
    state = BrandingData.empty;
    try {
      await ref.read(appStateStoreProvider).writeBranding(BrandingData.empty);
    } on Object {
      // best effort
    }
  }
}

final brandingProvider =
    NotifierProvider<BrandingNotifier, BrandingData>(BrandingNotifier.new);

/// Displayed company name, or the product name before setup.
String brandName(BrandingData b) => b.name ?? AppConstants.appName;

/// Logo (when cached) above/next to the company name.
class BrandHeader extends ConsumerWidget {
  const BrandHeader({super.key, this.logoSize = 72, this.style});
  final double logoSize;
  final TextStyle? style;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final b = ref.watch(brandingProvider);
    final logo = b.logo;
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (logo != null) ...[
          Image.memory(
            logo,
            height: logoSize,
            fit: BoxFit.contain,
            gaplessPlayback: true,
            excludeFromSemantics: true,
            errorBuilder: (_, __, ___) => const SizedBox.shrink(),
          ),
          const SizedBox(height: 12),
        ],
        Semantics(
          header: true,
          child: Text(
            brandName(b),
            textAlign: TextAlign.center,
            style: style ?? Theme.of(context).textTheme.headlineMedium,
          ),
        ),
      ],
    );
  }
}

/// Small logo + name for app bars.
class BrandTitle extends ConsumerWidget {
  const BrandTitle({super.key, this.fallback});
  final String? fallback;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final b = ref.watch(brandingProvider);
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (b.logo != null) ...[
          Image.memory(b.logo!, height: 28, excludeFromSemantics: true,
              errorBuilder: (_, __, ___) => const SizedBox.shrink(),),
          const SizedBox(width: 8),
        ],
        Flexible(
          child: Text(b.name ?? fallback ?? AppConstants.appName,
              overflow: TextOverflow.ellipsis,),
        ),
      ],
    );
  }
}
