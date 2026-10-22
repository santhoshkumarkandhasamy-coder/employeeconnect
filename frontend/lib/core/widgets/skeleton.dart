import 'package:flutter/material.dart';

/// Static placeholder block for skeleton loading screens (no animation, so it
/// is cheap and does not keep tests from settling).
class SkeletonBox extends StatelessWidget {
  const SkeletonBox({super.key, this.height = 16, this.width, this.radius = 8});
  final double height;
  final double? width;
  final double radius;

  @override
  Widget build(BuildContext context) => ExcludeSemantics(
        child: Container(
          height: height,
          width: width,
          decoration: BoxDecoration(
            color: Theme.of(context).colorScheme.surfaceContainerHighest,
            borderRadius: BorderRadius.circular(radius),
          ),
        ),
      );
}

/// A card-shaped skeleton with [lines] text lines.
class SkeletonCard extends StatelessWidget {
  const SkeletonCard({super.key, this.lines = 2, this.label = 'Loading'});
  final int lines;
  final String label;

  @override
  Widget build(BuildContext context) => Semantics(
        label: label,
        child: Card(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const SkeletonBox(width: 140, height: 18),
                for (var i = 0; i < lines; i++) ...[
                  const SizedBox(height: 10),
                  const SkeletonBox(),
                ],
              ],
            ),
          ),
        ),
      );
}
