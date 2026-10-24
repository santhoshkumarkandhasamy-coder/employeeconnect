import 'package:flutter/cupertino.dart' show CupertinoPageTransitionsBuilder;
import 'package:flutter/material.dart';

/// Material 3, system fonts, no assets.
abstract final class AppTheme {
  /// Brand colours taken from the company logo (navy and orange).
  static const Color _seed = Color(0xFF044C84);
  static const Color _accent = Color(0xFFF47424);

  static ThemeData get light => _build(Brightness.light);
  static ThemeData get dark => _build(Brightness.dark);

  static ThemeData _build(Brightness b) {
    // fromSeed gives a mid-tone of the seed, which hardly differs from a
    // generic blue, so the logo navy is set exactly in light mode. Orange text
    // on white fails contrast, so selection highlights use a deeper orange
    // and the logo orange stays decorative (tertiary).
    final base = ColorScheme.fromSeed(seedColor: _seed, brightness: b);
    final scheme = b == Brightness.light
        ? base.copyWith(
            primary: _seed,
            onPrimary: Colors.white,
            primaryContainer: const Color(0xFFD6E6F7),
            onPrimaryContainer: const Color(0xFF021F3A),
            secondary: const Color(0xFFC2530A),
            onSecondary: Colors.white,
            secondaryContainer: const Color(0xFFFFDCC8),
            onSecondaryContainer: const Color(0xFF4A1C00),
            tertiary: _accent,
            onTertiary: Colors.white,
          )
        : base.copyWith(
            primary: const Color(0xFF9DC4EC),
            onPrimary: const Color(0xFF00294B),
            secondary: const Color(0xFFFFB48A),
            onSecondary: const Color(0xFF4A1C00),
            secondaryContainer: const Color(0xFF7A3500),
            onSecondaryContainer: const Color(0xFFFFDCC8),
            tertiary: _accent,
            onTertiary: Colors.white,
          );
    return ThemeData(
      useMaterial3: true,
      colorScheme: scheme,
      visualDensity: VisualDensity.standard,
      inputDecorationTheme: InputDecorationTheme(
        border: OutlineInputBorder(borderRadius: BorderRadius.circular(12)),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          minimumSize: const Size.fromHeight(52),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        ),
      ),
      pageTransitionsTheme: const PageTransitionsTheme(builders: <TargetPlatform, PageTransitionsBuilder>{
        TargetPlatform.android: ZoomPageTransitionsBuilder(),
        TargetPlatform.iOS: CupertinoPageTransitionsBuilder(),
      },),
    );
  }
}
