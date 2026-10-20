/** Summarize the resolved plan, including overrides from the school settings. */
export function describeClassCopies(plan: number[]): string {
  if (!plan.length) return "尚未设置班级份数";
  const groups = new Map<number, number[]>();
  plan.forEach((copies, index) => {
    const classes = groups.get(copies) || [];
    classes.push(index + 1);
    groups.set(copies, classes);
  });
  if (groups.size === 1) return `共${plan.length}个班，每班${plan[0]}份`;
  const descriptions = [...groups].map(([copies, classes]) => {
    const ranges: string[] = [];
    for (let i = 0; i < classes.length; i++) {
      const start = classes[i];
      let end = start;
      while (i + 1 < classes.length && classes[i + 1] === end + 1) {
        end = classes[++i];
      }
      ranges.push(start === end ? `${start}班` : `${start}–${end}班`);
    }
    return `${ranges.join("、")}${classes.length > 1 ? "各" : ""}${copies}份`;
  });
  return `共${plan.length}个班：${descriptions.join("；")}`;
}
