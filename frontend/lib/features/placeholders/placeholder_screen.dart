import 'package:flutter/material.dart';

import '../../core/widgets/state_views.dart';

/// Stand-in for features built in later plans (02-10).
class PlaceholderScreen extends StatelessWidget {
  const PlaceholderScreen({required this.title, super.key});
  final String title;

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: Text(title)),
        body: EmptyView(
          icon: Icons.construction_outlined,
          message: '$title is coming soon.',
        ),
      );
}
